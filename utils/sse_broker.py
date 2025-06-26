# utils/sse_broker.py
import queue

class MessageAnnouncer:
    """
    A simple in-memory broker for Server-Sent Events (SSE).
    This manages multiple listeners in a single-process environment.
    """
    def __init__(self):
        self.listeners = []

    def listen(self):
        """
        Creates a new queue for a client and returns it.
        The generator yields messages as they are announced.
        """
        q = queue.Queue(maxsize=10) # Use a queue to handle messages
        self.listeners.append(q)
        while True:
            try:
                # Block until a message is available, with a timeout to prevent hanging
                msg = q.get(timeout=30)
                yield msg
            except queue.Empty:
                # Send a comment to keep the connection alive if no message for 30s
                yield ": keep-alive\n\n"
            except GeneratorExit:
                # The client has disconnected.
                self.listeners.remove(q)
                return

    def announce(self, msg):
        """
        Pushes a new message to all connected listeners.
        """
        # We are formatting the SSE message here
        formatted_msg = f"data: {msg}\n\n"
        for i in reversed(range(len(self.listeners))):
            try:
                self.listeners[i].put_nowait(formatted_msg)
            except queue.Full:
                # If a client's queue is full, they are likely disconnected.
                # Remove them to prevent blocking.
                del self.listeners[i]

# Create a single global instance of our announcer
announcer = MessageAnnouncer()