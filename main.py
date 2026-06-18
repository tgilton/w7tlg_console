"""
W7TLG Console — Main entry point

Usage:
    python main.py              # default: localhost:8000
    python main.py --port 8080  # custom port
    python main.py --host 0.0.0.0  # accessible from other devices on LAN
"""

import argparse
import logging
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='W7TLG Station Console')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host to bind (use 0.0.0.0 for LAN access)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Port to listen on')
    parser.add_argument('--reload', action='store_true',
                        help='Auto-reload on code changes (development)')
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  W7TLG Station Console")
    print(f"  http://{args.host}:{args.port}")
    print(f"  Start rigctld first: ~/start_rigctld.sh")
    print(f"{'='*50}\n")

    uvicorn.run(
        "dashboard.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
