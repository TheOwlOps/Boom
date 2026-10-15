"""Gateway: every message from outside the process enters here.

Channels are either polled (`GatewayDaemon.poll_telegram`) or pushed in over HTTP
(`IngressServer`), and both paths end in `GatewayDaemon.handle_incoming_message`, which is
where the owner guard and the delivery ledger live. It sits at the repo root rather than under
`agent/src` because it is a peer of the agent, not part of it: the agent loop is one consumer of
what arrives here.
"""

from gateway.daemon import GatewayDaemon
from gateway.delivery_ledger import DeliveryLedger
from gateway.discord import DiscordChannel
from gateway.ingress import IngressServer

__all__ = ["GatewayDaemon", "DeliveryLedger", "DiscordChannel", "IngressServer"]
