from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from datetime import datetime
import os
import json
import time
# Trigger CI workflow - final build with K8s labels + DB migration + IRSA
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Database configuration
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@postgres-service:5432/qprofiles')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy(app)

# Store for SSE connections
sse_connections = []

# User model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=False)
    aws_user_id = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'aws_user_id': self.aws_user_id,
            'created_at': self.created_at.isoformat()
        }

# Single password authentication
LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'vending123')

def get_aws_caller_identity():
    """Get AWS STS caller identity"""
    try:
        sts = boto3.client('sts')
        response = sts.get_caller_identity()
        return {
            'account': response.get('Account'),
            'arn': response.get('Arn'),
            'user_id': response.get('UserId')
        }
    except Exception as e:
        return {'error': str(e)}

def broadcast_sse(event, data):
    """Broadcast data to all SSE connections"""
    message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    # Send to active connections (remove error-prone closed check)
    global sse_connections
    active_connections = []
    for conn in sse_connections:
        try:
            conn.write(message)
            active_connections.append(conn)
        except:
            # Connection is closed, skip it
            pass
    sse_connections = active_connections

@app.route('/events')
def events():
    """Server-Sent Events endpoint"""
    def event_stream():
        # Send initial connection event
        yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"
        
        # Keep connection alive
        while True:
            yield f"event: heartbeat\ndata: {json.dumps({'timestamp': time.time()})}\n\n"
            time.sleep(30)
    
    response = Response(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    sse_connections.append(response)
    return response

@app.route('/')
def index():
    if 'logged_in' in session:
        users = User.query.order_by(User.created_at.desc()).all()
        caller_identity = get_aws_caller_identity()
        return render_template('dashboard.html', users=users, caller_identity=caller_identity)
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form['password']
        if password == LOGIN_PASSWORD:
            session['logged_in'] = True
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid password!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/api/users', methods=['POST'])
def create_user():
    if 'logged_in' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')
    
    if not username or not email:
        return jsonify({'error': 'Username and email required'}), 400
    
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 400
    
    try:
        # Create user in AWS Identity Center
        aws_user_id = create_identity_center_user(username, email, first_name, last_name)
        
        # Create user in local database
        user = User(username=username, email=email, aws_user_id=aws_user_id)
        db.session.add(user)
        db.session.commit()
        
        # Broadcast via SSE
        broadcast_sse('user_created', user.to_dict())
        
        return jsonify(user.to_dict()), 201
        
    except Exception as e:
        return jsonify({'error': f'Failed to create user: {str(e)}'}), 500

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    try:
        user = User.query.get_or_404(user_id)
        
        # Delete from AWS Identity Center if user_id exists
        if user.user_id:
            try:
                client_ic = boto3.client('identitystore', region_name='eu-central-1')
                client_ic.delete_user(
                    IdentityStoreId='d-99676ce775',
                    UserId=user.user_id
                )
            except Exception as e:
                print(f"Warning: Failed to delete from Identity Center: {e}")
        
        # Delete from database
        db.session.delete(user)
        db.session.commit()
        
        # Broadcast via SSE
        broadcast_sse('user_deleted', {'id': user_id})
        
        return jsonify({'message': 'User deleted successfully'}), 200
        
    except Exception as e:
        return jsonify({'error': f'Failed to delete user: {str(e)}'}), 500

@app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
def reset_user_password(user_id):
    """Trigger password reset for existing user"""
    try:
        user = User.query.get_or_404(user_id)
        
        if not user.user_id:
            return jsonify({'error': 'User not found in Identity Center'}), 400
        
        # Note: AWS Identity Center doesn't have a direct API to resend invitation emails
        # This would typically be done through the AWS Console or by recreating the user
        # For now, we'll return instructions for manual reset
        
        return jsonify({
            'message': f'Password reset instructions: Go to AWS Identity Center console and resend invitation for user {user.username}',
            'console_url': 'https://eu-central-1.console.aws.amazon.com/singlesignon/identity/home',
            'user_id': user.user_id
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'Failed to reset password: {str(e)}'}), 500

def create_identity_center_user(username, email, first_name, last_name):
    """Create user in AWS Identity Center and prepare for email verification"""
    try:
        client_ic = boto3.client('identitystore', region_name='eu-central-1')
        
        # Create user
        response = client_ic.create_user(
            IdentityStoreId='d-99676ce775',  # Correct Identity Store ID in Frankfurt
            UserName=username,
            Name={
                'GivenName': first_name or username,
                'FamilyName': last_name or 'User'
            },
            DisplayName=f"{first_name} {last_name}".strip() or username,
            Emails=[
                {
                    'Value': email,
                    'Type': 'work',
                    'Primary': True
                }
            ]
        )
        
        user_id = response['UserId']
        print(f"✅ User {username} created in Identity Center with ID: {user_id}")
        
        # Note: AWS Identity Center automatically sends invitation email when user is created
        # The user will receive an email to set up their password and activate their account
        print(f"📧 Invitation email will be sent to {email} automatically by AWS Identity Center")
        
        return user_id
        
    except Exception as e:
        print(f"Error creating Identity Center user: {e}")
        raise e

@app.route('/health')
def health():
    return {'status': 'healthy', 'service': 'q-profile-vending'}

def init_db_with_retry(max_retries=30, delay=2):
    """Initialize database with retry logic - only create tables if they don't exist"""
    for attempt in range(max_retries):
        try:
            with app.app_context():
                # Only create tables if they don't exist (preserve existing data)
                db.create_all()
            print("Database initialized successfully")
            return
        except Exception as e:
            print(f"Database connection attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                print("Failed to connect to database after all retries")
                raise

if __name__ == '__main__':
    init_db_with_retry()
    app.run(host='0.0.0.0', port=8080, debug=False)
