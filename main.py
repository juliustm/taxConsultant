from flask import Flask, request

# Initialize the Flask application
app = Flask(__name__)

# Define the endpoint for the webhook
@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'POST':
        data = request.json  # Retrieve JSON data sent to the webhook
        print("Received POST request:", data)
        return "POST request received", 200
    elif request.method == 'GET':
        print("Received GET request")
        return "GET request received", 200

# Run the application on port 80
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
