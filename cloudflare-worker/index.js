import { DurableObject } from "cloudflare:workers";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type"
};
const json = (data, status = 200) => Response.json(data, { status, headers: CORS });

export class GameRoom extends DurableObject {
  constructor(ctx, env) { super(ctx, env); this.ctx = ctx; }

  async fetch(request) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("WebSocket required", { status: 426, headers: CORS });
    }
    const sockets = this.ctx.getWebSockets();
    if (sockets.length >= 4) return new Response("Sala llena", { status: 403, headers: CORS });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    const id = crypto.randomUUID();
    server.serializeAttachment({ id });
    this.ctx.acceptWebSocket(server);

    const current = this.ctx.getWebSockets();
    let hostId = await this.ctx.storage.get("hostId");
    const connectedIds = current.map(ws => {
      try { return ws.deserializeAttachment()?.id; } catch { return null; }
    }).filter(Boolean);
    if (!hostId || !connectedIds.includes(hostId)) {
      hostId = id;
      await this.ctx.storage.put("hostId", hostId);
    }

    server.send(JSON.stringify({ type: "welcome", id, hostId, isHost: id === hostId, players: current.length, maxPlayers: 4 }));
    this.broadcast({ type: "player-joined", id, hostId, players: current.length }, server);
    return new Response(null, { status: 101, webSocket: client });
  }

  broadcast(data, except = null) {
    const message = JSON.stringify(data);
    for (const ws of this.ctx.getWebSockets()) {
      if (ws === except) continue;
      try { ws.send(message); } catch {}
    }
  }

  async webSocketMessage(socket, message) {
    let data;
    try { data = JSON.parse(typeof message === "string" ? message : await message.text()); } catch { return; }
    if (!data || typeof data !== "object" || typeof data.type !== "string") return;
    let senderId;
    try { senderId = socket.deserializeAttachment()?.id; } catch { return; }
    if (!senderId || data.type === "host-changed") return;
    const outgoing = { ...data, senderId };
    if (outgoing.type === "player-state") outgoing.id = senderId;
    this.broadcast(outgoing, socket);
  }

  async webSocketClose(socket) {
    let id = null;
    try { id = socket.deserializeAttachment()?.id || null; } catch {}
    const remaining = this.ctx.getWebSockets().filter(ws => ws !== socket);
    let hostId = await this.ctx.storage.get("hostId");
    const wasHost = !!id && id === hostId;
    if (wasHost) {
      hostId = null;
      if (remaining.length) {
        try { hostId = remaining[0].deserializeAttachment()?.id || null; } catch {}
      }
      if (hostId) await this.ctx.storage.put("hostId", hostId);
      else await this.ctx.storage.delete("hostId");
    }
    this.broadcast({ type: "player-left", id, players: remaining.length, hostId, hostChanged: wasHost });
    if (wasHost && hostId) this.broadcast({ type: "host-changed", hostId });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === "/") return json({ status: "online", game: "ABYSSAL REDO" });
    if (url.pathname === "/create") {
      const code = crypto.randomUUID().replace(/-/g, "").slice(0, 6).toUpperCase();
      return json({ code });
    }
    if (url.pathname.startsWith("/join/")) {
      const code = url.pathname.split("/")[2]?.toUpperCase();
      if (!code || !/^[A-F0-9]{6}$/.test(code)) return json({ error: "Código de sala inválido" }, 400);
      return env.ROOMS.get(env.ROOMS.idFromName(code)).fetch(request);
    }
    return json({ error: "Not found" }, 404);
  }
};
