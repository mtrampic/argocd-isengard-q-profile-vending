#!/usr/bin/env python3
"""
Simple SSE Test Server
Run this to test SSE functionality without the full application
"""

from flask import Flask, Response, render_template_string, jsonify
import json
import time
import threading

app = Flask(__name__)

# Simple in-memory storage
users = []
sse_events = []
user_counter = 1

def broadcast_sse(event, data):
    """Broadcast data to all SSE connections"""
    print(f"🔔 SSE broadcast: {event} - {data}")
    sse_events.append({
        'event': event,
        'data': data,
        'timestamp': time.time(),
        'id': len(sse_events)
    })
    if len(sse_events) > 50:
        sse_events.pop(0)

@app.route('/')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>SSE Test</title>
    <style>
        body { font-family: Arial; max-width: 800px; margin: 50px auto; padding: 20px; }
        .status { padding: 5px 10px; border-radius: 4px; margin: 10px 0; }
        .connected { background: #d4edda; color: #155724; }
        .disconnected { background: #f8d7da; color: #721c24; }
        button { padding: 10px 20px; margin: 5px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0056b3; }
        #log { background: #f8f9fa; padding: 10px; border-radius: 4px; height: 300px; overflow-y: scroll; margin: 20px 0; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; }
        th, td { padding: 8px; border: 1px solid #ddd; text-align: left; }
        th { background: #f8f9fa; }
        .highlight { background: #d4edda !important; transition: background 2s; }
    </style>
</head>
<body>
    <h1>SSE Functionality Test</h1>
    
    <div class="status disconnected" id="status">Disconnected</div>
    
    <div>
        <button onclick="addUser()">Add Test User</button>
        <button onclick="removeUser()">Remove Last User</button>
        <button onclick="clearLog()">Clear Log</button>
    </div>
    
    <h3>Users (<span id="count">0</span>)</h3>
    <table>
        <thead><tr><th>ID</th><th>Username</th><th>Created</th></tr></thead>
        <tbody id="users-table"></tbody>
    </table>
    
    <h3>Event Log</h3>
    <div id="log"></div>

    <script>
        const statusEl = document.getElementById('status');
        const logEl = document.getElementById('log');
        const tableEl = document.getElementById('users-table');
        const countEl = document.getElementById('count');
        
        function log(message) {
            const time = new Date().toLocaleTimeString();
            logEl.innerHTML += `<div>[${time}] ${message}</div>`;
            logEl.scrollTop = logEl.scrollHeight;
        }
        
        function connectSSE() {
            log('🔌 Connecting to SSE...');
            const eventSource = new EventSource('/events');
            
            eventSource.onopen = function() {
                log('✅ SSE Connected');
                statusEl.textContent = 'Connected (SSE)';
                statusEl.className = 'status connected';
            };
            
            eventSource.onerror = function(error) {
                log('❌ SSE Error: ' + error);
                statusEl.textContent = 'Disconnected';
                statusEl.className = 'status disconnected';
            };
            
            eventSource.addEventListener('connected', function(e) {
                log('🎉 Connected event: ' + e.data);
            });
            
            eventSource.addEventListener('user_created', function(e) {
                log('👤 User created: ' + e.data);
                const user = JSON.parse(e.data);
                addUserToTable(user);
            });
            
            eventSource.addEventListener('user_deleted', function(e) {
                log('🗑️ User deleted: ' + e.data);
                const data = JSON.parse(e.data);
                removeUserFromTable(data.id);
            });
            
            eventSource.addEventListener('heartbeat', function(e) {
                log('💓 Heartbeat: ' + e.data);
            });
        }
        
        function addUserToTable(user) {
            const row = tableEl.insertRow(0);
            row.id = `user-${user.id}`;
            row.innerHTML = `<td>${user.id}</td><td>${user.username}</td><td>${user.created_at}</td>`;
            row.className = 'highlight';
            setTimeout(() => row.className = '', 2000);
            updateCount();
        }
        
        function removeUserFromTable(userId) {
            const row = document.getElementById(`user-${userId}`);
            if (row) {
                row.style.backgroundColor = '#f8d7da';
                setTimeout(() => { row.remove(); updateCount(); }, 1000);
            }
        }
        
        function updateCount() {
            countEl.textContent = tableEl.rows.length;
        }
        
        async function addUser() {
            try {
                const response = await fetch('/api/users', { method: 'POST' });
                if (!response.ok) log('❌ Failed to add user');
            } catch (error) {
                log('❌ Error adding user: ' + error);
            }
        }
        
        async function removeUser() {
            try {
                const response = await fetch('/api/users', { method: 'DELETE' });
                if (!response.ok) log('❌ Failed to remove user');
            } catch (error) {
                log('❌ Error removing user: ' + error);
            }
        }
        
        function clearLog() {
            logEl.innerHTML = '';
        }
        
        // Initialize
        connectSSE();
        log('🚀 Application started');
    </script>
</body>
</html>
    ''')

@app.route('/events')
def events():
    """Server-Sent Events endpoint"""
    connection_id = f"conn_{int(time.time() * 1000)}"
    print(f"🔌 New SSE connection: {connection_id}")
    
    def event_stream():
        yield f"event: connected\\ndata: {json.dumps({'status': 'connected'})}\n\n"
        
        last_event_index = len(sse_events)
        last_heartbeat = time.time()
        
        while True:
            try:
                # Send new events
                current_count = len(sse_events)
                if current_count > last_event_index:
                    for i in range(last_event_index, current_count):
                        event_data = sse_events[i]
                        yield f"event: {event_data['event']}\\ndata: {json.dumps(event_data['data'])}\n\n"
                    last_event_index = current_count
                
                # Heartbeat
                if time.time() - last_heartbeat > 30:
                    yield f"event: heartbeat\\ndata: {json.dumps({'timestamp': time.time()})}\n\n"
                    last_heartbeat = time.time()
                
                time.sleep(1)
            except GeneratorExit:
                print(f"🔌 Connection closed: {connection_id}")
                break
    
    response = Response(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/api/users', methods=['POST'])
def add_user():
    global user_counter
    user = {
        'id': user_counter,
        'username': f'user{user_counter}',
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    users.append(user)
    user_counter += 1
    
    broadcast_sse('user_created', user)
    return jsonify(user)

@app.route('/api/users', methods=['DELETE'])
def remove_user():
    if users:
        user = users.pop()
        broadcast_sse('user_deleted', {'id': user['id']})
        return jsonify({'message': 'User deleted'})
    return jsonify({'error': 'No users to delete'}), 400

if __name__ == '__main__':
    print("🚀 Starting SSE Test Server on http://localhost:5000")
    print("📖 Open multiple browser tabs to test SSE broadcasting")
    app.run(debug=True, port=5000)