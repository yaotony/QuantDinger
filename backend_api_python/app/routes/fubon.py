"""
Fubon market data sidecar status (read-only).

Fubon is a market-data-only source for TWStock / TWFutures: it is not a trading
venue, is not in the broker market policy and has no saved credentials. This
blueprint only exposes the sidecar connection status for the account center;
there are deliberately no connect, account, position or order endpoints.
"""

from flask import jsonify

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.fubon_status import get_fubon_status
from app.utils.auth import login_required

fubon_blp = Blueprint('fubon', __name__)


@fubon_blp.route('/status', methods=['GET'])
@login_required
def get_status():
    """Get the Fubon market data sidecar connection status."""
    return jsonify({"success": True, "data": get_fubon_status()})
