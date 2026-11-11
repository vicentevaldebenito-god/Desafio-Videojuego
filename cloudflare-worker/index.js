import { DurableObject } from "cloudflare:workers";

export class GameRoom extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("WebSocket required", { status: 426 });
    }

    const sockets = this.ctx.getWebSockets();
    if (sockets.length >= 4) {
      return new Response("Room is full", { status:  roomFullStatus() });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    const id = crypto.randomUUID();

    server.serializeAttachment({ id });
    this.ctx.acceptWebSocket(server);

    let hostId = await this.ctx.storage.get("hostId");
    if (!hostId) {
      hostId = id;
      await this.ctx.storage.put("hostId", hostId);
    }

    const current = this.ctx.getWebSockets();
    const players = current.map(ws => {
      try { return ws.deserializeAttachment()?.id; } catch { return null; }
    }).filter(Boolean);

    server.send(JSON.stringify({
      type: "welcome",
      id,
      hostId,
      isHost: id === hostId,
      players: current.length,
      maxPlayers: 4
    }));

    this.broadcast({
      type: "player-joined",
      id,
      hostId,
      players: current.length
    }, server);

    return new Response(null, { status: 101, webSocket: client });
  }

  broadcast(message, except = null) {
    const payload = JSON.stringify(message);
    for (const ws of this.ctx.getWebSockets()) {
      if (ws === except) continue;
      try { ws.send(payload); } catch {}
    }
  }

  async webSocketMessage(socket, message) {
    let data;
    try {
      data = JSON.parse(typeof message === "string" ? message : await message.text());
    } catch {
      return;
    }
    if (!data || typeof data !== "object" || typeof data.type !== "string") return;

    let senderId = null;
    try { senderId = socket.deserializeAttachment()?.id || null; } catch {}
    if (!senderId) return;

    const hostId = await this.ctx.storage.get("hostId");
    const outgoing = { ...data, senderId };

    // El servidor determina la identidad del jugador; el cliente no puede suplantarla.
    if (outgoing.type === "player-state") outgoing.id = senderId;

    // Por ahora el relay conserva los mensajes de juego y limita cambios de host al servidor.
    if (outgoing.type === "host-changed") return;

    this.broadcast(outgoing, socket);
  }

  async webSocketClose(socket) {
    let id = null;
    try { id = socket.deserializeAttachment()?.id || null; } catch {}

    const remaining = this.ctx.getWebSockets().filter(ws => ws !== socket);
    let hostId = await this.ctx.storage.get("hostId");

    if (id && id === hostId) {
      hostId = null;
      if (remaining.length) {
        try { hostId = remaining[0].deserializeAttachment()?.id || null; } catch {}
      }
      if (hostId) await this.ctx.storage.put("hostId", hostId);
      else await this.ctx.storage.delete("hostId");
    }

    this.broadcast({
      type: "player-left",
      id,
      players: remaining.length,
      hostId,
      hostChanged: !!id && id !== hostId
    });

    if (hostId) this.broadcast({ type: "host-changed", hostId });
  }

  async webSocketError(socket) {
    await this.webSocketClose(socket);
  }
}

function roomFullStatus() {
  return 409;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/") {
      return Response.json({ status: "online", game: "ABYSSAL REDO" });
    }

    if (url.pathname === "/create") {
      const code = crypto.randomUUID().replace(/-/g, "").slice(0, 6).toUpperCase();
      const id = env.ROOMS.idFromName(code);
      const stub = env.ROOMS.get(id);
      const response = await stub.fetch("https://room.internal/join");
      if (!response.ok) return new Response("Could not create room", { status: 500 });
      return Response.json({ code });
    }

    if (url.pathname.startsWith("/join/")) {
      const code = url.pathname.split("/")[2]?.toUpperCase();
      if (!code || !/^[A-F0-9]{6}$/.test(code)) {
        return new Response("Invalid room code", { status: 400 });
      }
      const id = env.ROOMS.idFromName(code);
      return env.ROOMS.get(id).fetch(request);
    }

    return new Response("Not found", { status: 404 });
  }
};
