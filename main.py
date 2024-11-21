from flask import Flask, request, jsonify

# Initialize the Flask application
app = Flask(__name__)

# Define the endpoint for the webhook
@app.route('/test', methods=['POST', 'GET'])
def webhook():
    if request.method == 'POST':
        try:
            # Check Content-Type header
            content_type = request.headers.get('Content-Type', '')
            
            if 'application/json' in content_type:
                data = request.json
            else:
                # Handle other content types as raw data
                data = request.get_data().decode('utf-8')
            
            print(f"Received POST request with Content-Type: {content_type}")
            print(f"Data: {data}")
            
            return jsonify({"message": "POST request received", "data": data}), 200
            
        except Exception as e:
            print(f"Error processing request: {str(e)}")
            return jsonify({"error": str(e)}), 400
            
    elif request.method == 'GET':
        print("Received GET request")
        return jsonify({"message": "GET request received"}), 200
