from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from datetime import datetime
import os
import json
import time
import queue
import threading
# Trigger CI workflow - final build with K8s labels + DB migration + IRSA + rebuild
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

# Store for SSE connections - using threading for real broadcast
import threading
sse_connections = []
sse_lock = threading.Lock()
sse_events = []  # Store events for all connections

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

def broadcast_sse(event, data):
    """Broadcast data to all SSE connections using thread-safe approach"""
    print(f"🔔 SSE broadcast: {event} - {data}")
    print(f"📊 Current active connections: {len(sse_connections)}")
    
    # Add event to global events list with timestamp (for new connections)
    event_entry = {
        'event': event,
        'data': data,
        'timestamp': time.time(),
        'id': len(sse_events)
    }
    
    with sse_lock:
        sse_events.append(event_entry)
        print(f"✅ Event added to broadcast list: {event_entry}")
        
        # Keep only last 100 events to prevent memory growth
        if len(sse_events) > 100:
            removed = sse_events.pop(0)
            print(f"🗑️ Removed old event: {removed}")
        
        # Immediately notify all active connections
        dead_connections = []
        for i, connection in enumerate(sse_connections):
            try:
                if hasattr(connection, 'put_nowait'):
                    connection.put_nowait(event_entry)
                    print(f"📤 Event sent to connection {i}")
            except:
                dead_connections.append(i)
                print(f"💀 Dead connection detected: {i}")
        
        # Remove dead connections
        for i in reversed(dead_connections):
            sse_connections.pop(i)
            print(f"🗑️ Removed dead connection {i}")

@app.route('/events')
def events():
    """Server-Sent Events endpoint with proper multi-user broadcasting"""
    connection_id = f"conn_{int(time.time() * 1000)}_{threading.current_thread().ident}"
    print(f"🔌 New SSE connection: {connection_id}")
    
    # Create a queue for this specific connection
    connection_queue = queue.Queue()
    
    with sse_lock:
        sse_connections.append(connection_queue)
        connection_index = len(sse_connections) - 1
        print(f"📋 Connection registered at index {connection_index}, total connections: {len(sse_connections)}")
    
    def event_stream():
        try:
            # Send initial connection event
            initial_msg = f"event: connected\ndata: {json.dumps({'status': 'connected', 'connection_id': connection_id})}\n\n"
            print(f"📤 Sending initial connection event to {connection_id}")
            yield initial_msg
            
            last_heartbeat = time.time()
            last_event_index = len(sse_events)  # Start from current position
            print(f"📍 {connection_id} starting from event index: {last_event_index}")
            
            # Send any existing events since connection started
            if last_event_index > 0:
                print(f"📨 {connection_id} sending {last_event_index} historical events")
                for i in range(max(0, last_event_index - 10), last_event_index):  # Last 10 events
                    if i < len(sse_events):
                        event_data = sse_events[i]
                        message = f"event: {event_data['event']}\ndata: {json.dumps(event_data['data'])}\n\n"
                        yield message
            
            while True:
                try:
                    # Check for new events in this connection's queue (real-time broadcasts)
                    try:
                        event_data = connection_queue.get_nowait()
                        message = f"event: {event_data['event']}\ndata: {json.dumps(event_data['data'])}\n\n"
                        print(f"📤 {connection_id} sending real-time: {event_data['event']} - {event_data['data']}")
                        yield message
                        continue
                    except queue.Empty:
                        pass
                    
                    # Also check for any events added to global list (fallback)
                    current_event_count = len(sse_events)
                    if current_event_count > last_event_index:
                        print(f"📨 {connection_id} sending {current_event_count - last_event_index} fallback events")
                        for i in range(last_event_index, current_event_count):
                            if i < len(sse_events):
                                event_data = sse_events[i]
                                message = f"event: {event_data['event']}\ndata: {json.dumps(event_data['data'])}\n\n"
                                print(f"📤 {connection_id} sending fallback: {event_data['event']} - {event_data['data']}")
                                yield message
                        last_event_index = current_event_count
                    
                    # Send heartbeat every 30 seconds
                    current_time = time.time()
                    if current_time - last_heartbeat > 30:
                        heartbeat_msg = f"event: heartbeat\ndata: {json.dumps({'timestamp': current_time, 'connection_id': connection_id})}\n\n"
                        print(f"💓 {connection_id} sending heartbeat")
                        yield heartbeat_msg
                        last_heartbeat = current_time
                    
                    time.sleep(0.1)  # Much faster polling for real-time feel
                    
                except GeneratorExit:
                    print(f"🔌 SSE connection closed: {connection_id}")
                    break
                except Exception as e:
                    print(f"❌ SSE error for {connection_id}: {e}")
                    break
        finally:
            # Clean up connection
            with sse_lock:
                try:
                    sse_connections.remove(connection_queue)
                    print(f"🧹 Cleaned up connection {connection_id}")
                except ValueError:
                    pass
    
    response = Response(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/')
def index():
    if 'logged_in' in session:
        users = User.query.order_by(User.created_at.desc()).all()
        return render_template('dashboard.html', users=users)
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

@app.route('/api/users', methods=['GET'])
def get_users():
    """Get all users as JSON"""
    if 'logged_in' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify([user.to_dict() for user in users])

@app.route('/api/sse-status', methods=['GET'])
def sse_status():
    """Get SSE connection status for debugging"""
    if 'logged_in' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    with sse_lock:
        return jsonify({
            'active_connections': len(sse_connections),
            'total_events': len(sse_events),
            'recent_events': sse_events[-5:] if sse_events else []
        })

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
        
        # Delete from AWS Identity Center if aws_user_id exists
        if user.aws_user_id:
            try:
                client_ic = boto3.client('identitystore', region_name='eu-central-1')
                client_ic.delete_user(
                    IdentityStoreId='d-99676ce775',
                    UserId=user.aws_user_id
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
    """Reset user password by recreating the user (triggers new invitation email)"""
    try:
        user = User.query.get_or_404(user_id)
        
        if not user.aws_user_id:
            return jsonify({'error': 'User not found in Identity Center'}), 400
        
        # Identity Center doesn't have admin_reset_user_password
        # The only way to trigger a new invitation email is to delete and recreate the user
        try:
            client_ic = boto3.client('identitystore', region_name='eu-central-1')
            
            # Get current user details before deletion
            current_user = client_ic.describe_user(
                IdentityStoreId='d-99676ce775',
                UserId=user.aws_user_id
            )
            
            # Delete existing user
            client_ic.delete_user(
                IdentityStoreId='d-99676ce775',
                UserId=user.aws_user_id
            )
            
            # Recreate user (this triggers new invitation email)
            response = client_ic.create_user(
                IdentityStoreId='d-99676ce775',
                UserName=current_user['UserName'],
                Name=current_user['Name'],
                DisplayName=current_user.get('DisplayName', current_user['UserName']),
                Emails=current_user.get('Emails', [])
            )
            
            # Update database with new AWS user ID
            user.aws_user_id = response['UserId']
            db.session.commit()
            
            return jsonify({
                'message': f'Password reset successful. New invitation email sent to {user.email}',
                'new_aws_user_id': response['UserId']
            }), 200
            
        except Exception as e:
            return jsonify({'error': f'Failed to reset password via Identity Center: {str(e)}'}), 500
        
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
