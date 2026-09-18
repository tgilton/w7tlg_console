"""
W7TLG Console — Main entry point

Usage:
    python main.py              # default: localhost:8000
    python main.py --port 8080  # custom port
    python main.py --host 0.0.0.0  # accessible from other devices on LAN
"""

import argparse
import asyncio
import logging

import uvicorn
from dotenv import load_dotenv

# Must happen before any module that reads os.environ at import/startup time
# (e.g. the AI advisor's ANTHROPIC_API_KEY) — .env is not auto-sourced by
# the shell just because it's present in the directory. os.environ changes
# here are inherited by uvicorn's --reload subprocess too, since child
# processes inherit the parent's environment.
load_dotenv()

# Must happen before either RX's AudioDemodulator loads its DeepFilterNet
# model (sdr/audio_demod.py). By default PyTorch's CPU backend spawns
# intra-op threads across every logical core for each inference call; with
# two independent AudioDemodulator instances (one per receiver) each running
# enhance() 10x/sec, both try to claim all cores simultaneously, causing
# thread-pool oversubscription and audio dropouts under dual-RX + DNR load.
try:
    import torch
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
except ImportError:
    pass  # DNR deps not installed — nothing to cap

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)

logger = logging.getLogger(__name__)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='W7TLG Station Console')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host to bind (use 0.0.0.0 for LAN access)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Port to listen on')
    parser.add_argument('--reload', action='store_true',
                        help='Auto-reload on code changes (development)')
    args = parser.parse_args()

    # Signal handling is left entirely to uvicorn, which installs graceful
    # shutdown handlers for BOTH SIGINT (Ctrl+C) and SIGTERM (kill) and runs
    # the app's lifespan shutdown on either — that's what releases the SDR
    # (sdrplay_api_Uninit) and closes the ACOM serial port cleanly. A custom
    # SIGTERM handler used to live here and just called sys.exit(0), which
    # preempted uvicorn's handler so lifespan shutdown never ran on `kill` —
    # leaving the RSPdx-R2 stuck in the streaming state, so the next start
    # failed with sdrplay_api_Fail until the device was physically replugged.

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
