from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import UploadBatch


class BatchProgressConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        self.batch_public_id = self.scope["url_route"]["kwargs"]["public_id"]

        if not await self._user_can_view_batch():
            await self.close(code=4403)
            return

        self.group_name = f"batch_{self.batch_public_id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def pipeline_event(self, event):
        await self.send_json(event["event"])

    @database_sync_to_async
    def _user_can_view_batch(self):
        user = self.scope["user"]

        if not user or not user.is_authenticated:
            return False

        return UploadBatch.objects.filter(
            public_id=self.batch_public_id,
            organization=user.organization,
        ).exists()


websocket_urlpatterns = [
    path(
        "ws/batches/<uuid:public_id>/",
        BatchProgressConsumer.as_asgi(),
    ),
]

#UI

# import { useEffect, useState } from "react";
#
# const BatchProgress = ({ batchId }) => {
#   const [event, setEvent] = useState(null);
#   const [connected, setConnected] = useState(false);
#
#   useEffect(() => {
#     if (!batchId) return;
#
#     const protocol =
#       window.location.protocol === "https:" ? "wss" : "ws";
#
#     const socket = new WebSocket(
#       `${protocol}://${window.location.host}/ws/batches/${batchId}/`
#     );
#
#     socket.onopen = () => {
#       console.log("WebSocket connected");
#       setConnected(true);
#     };
#
#     socket.onmessage = (event) => {
#       const data = JSON.parse(event.data);
#
#       console.log("Pipeline event:", data);
#
#       setEvent(data);
#     };
#
#     socket.onerror = (error) => {
#       console.error("WebSocket error:", error);
#     };
#
#     socket.onclose = (event) => {
#       console.log("WebSocket closed:", event.code);
#       setConnected(false);
#     };
#
#     return () => {
#       socket.close();
#     };
#   }, [batchId]);
#
#   return (
#     <div>
#       <p>
#         Status: {connected ? "Connected" : "Disconnected"}
#       </p>
#
#       {event && (
#         <pre>
#           {JSON.stringify(event, null, 2)}
#         </pre>
#       )}
#     </div>
#   );
# };
#
# export default BatchProgress;
# {
#   "eventType": "file.extracting",
#   "fileId": "...",
#   "batchId": "...",
#   "payload": {
#     "percent": 45
#   }
# }``