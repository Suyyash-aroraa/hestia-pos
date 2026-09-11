import queue
from flask import Blueprint, Response, current_app

bp = Blueprint('events', __name__, url_prefix='/api/events')

@bp.route('/pos')
def pos_stream():
    from app import sse_clients

    app = current_app._get_current_object()
    client_queue = queue.Queue(maxsize=100)
    sse_clients['pos'].append(client_queue)

    def generate():
        try:
            with app.app_context():
                pass
            while True:
                try:
                    data = client_queue.get(timeout=5)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ': keep-alive\n\n'
        finally:
            try:
                sse_clients['pos'].remove(client_queue)
            except ValueError:
                pass

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    return response
