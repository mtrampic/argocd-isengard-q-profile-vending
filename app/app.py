from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
import boto3
from kubernetes import client, config

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Database configuration
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@postgres-service:5432/qprofiles')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

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

# User credentials
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
PARTICIPANT_PASSWORD = os.environ.get('PARTICIPANT_PASSWORD', 'participant123')

@app.route('/')
def index():
    if 'logged_in' in session:
        users = User.query.order_by(User.created_at.desc()).all()
        return render_template('dashboard.html', users=users, user_role=session.get('user_role'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form['password']
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['user_role'] = 'admin'
            flash('Admin login successful!', 'success')
            return redirect(url_for('index'))
        elif password == PARTICIPANT_PASSWORD:
            session['logged_in'] = True
            session['user_role'] = 'participant'
            flash('Participant login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid password!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/api/aws-credentials', methods=['POST'])
def update_aws_credentials():
    if session.get('user_role') != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json()
    access_key = data.get('access_key')
    secret_key = data.get('secret_key')
    identity_store_id = data.get('identity_store_id')
    
    if not all([access_key, secret_key, identity_store_id]):
        return jsonify({'error': 'All AWS credentials required'}), 400
    
    try:
        # Update Kubernetes secret
        update_k8s_secret(access_key, secret_key, identity_store_id)
        return jsonify({'message': 'AWS credentials updated successfully'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to update credentials: {str(e)}'}), 500

@app.route('/api/users', methods=['POST'])
def create_user():
    if 'logged_in' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if session.get('user_role') != 'participant':
        return jsonify({'error': 'Participant access required'}), 403
    
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
        
        # Emit real-time update
        socketio.emit('user_created', user.to_dict())
        
        return jsonify(user.to_dict()), 201
        
    except Exception as e:
        return jsonify({'error': f'Failed to create user: {str(e)}'}), 500

def update_k8s_secret(access_key, secret_key, identity_store_id):
    """Update Kubernetes secret with AWS credentials"""
    try:
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        
        secret_data = {
            'AWS_ACCESS_KEY_ID': access_key,
            'AWS_SECRET_ACCESS_KEY': secret_key,
            'IDENTITY_STORE_ID': identity_store_id
        }
        
        # Create or update secret
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name='aws-credentials'),
            string_data=secret_data
        )
        
        try:
            v1.patch_namespaced_secret(name='aws-credentials', namespace='default', body=secret)
        except:
            v1.create_namespaced_secret(namespace='default', body=secret)
            
    except Exception as e:
        print(f"Error updating K8s secret: {e}")
        raise e

def get_aws_credentials():
    """Get AWS credentials from Kubernetes secret"""
    try:
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(name='aws-credentials', namespace='default')
        
        return {
            'access_key': secret.data.get('AWS_ACCESS_KEY_ID', '').decode('base64') if secret.data.get('AWS_ACCESS_KEY_ID') else None,
            'secret_key': secret.data.get('AWS_SECRET_ACCESS_KEY', '').decode('base64') if secret.data.get('AWS_SECRET_ACCESS_KEY') else None,
            'identity_store_id': secret.data.get('IDENTITY_STORE_ID', '').decode('base64') if secret.data.get('IDENTITY_STORE_ID') else None
        }
    except:
        return None

def create_identity_center_user(username, email, first_name, last_name):
    """Create user in AWS Identity Center using boto3"""
    creds = get_aws_credentials()
    if not creds or not all(creds.values()):
        raise Exception("AWS credentials not configured. Admin must set credentials first.")
    
    try:
        client_ic = boto3.client(
            'identitystore',
            region_name='us-east-1',
            aws_access_key_id=creds['access_key'],
            aws_secret_access_key=creds['secret_key']
        )
        
        response = client_ic.create_user(
            IdentityStoreId=creds['identity_store_id'],
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
        
        return response['UserId']
        
    except Exception as e:
        print(f"Error creating Identity Center user: {e}")
        raise e

@app.route('/health')
def health():
    return {'status': 'healthy', 'service': 'q-profile-vending'}

@socketio.on('connect')
def handle_connect():
    if 'logged_in' in session:
        emit('connected', {'status': 'Connected to Q Profile Vending'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, host='0.0.0.0', port=8080, debug=False)
