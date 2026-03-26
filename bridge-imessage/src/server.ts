/**
 * WebSocket server bridging Python ↔ iMessage SDKs.
 * Binds to 127.0.0.1 only for security.
 */

import { WebSocketServer, WebSocket } from 'ws';
import { LocalClient } from './local-client.js';
import { RemoteClient } from './remote-client.js';
import type { BridgeConfig, IMessageClient, InboundMessage } from './types.js';

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

interface SendMediaCommand {
  type: 'send_media';
  to: string;
  filePath: string;
  mimetype: string;
  caption?: string;
  fileName?: string;
}

interface SendReactionCommand {
  type: 'send_reaction';
  chatGuid: string;
  messageGuid: string;
  reaction: string;
}

interface StartTypingCommand {
  type: 'start_typing';
  chatGuid: string;
}

interface EditMessageCommand {
  type: 'edit_message';
  messageGuid: string;
  text: string;
}

interface ConfigCommand {
  type: 'config';
  local: boolean;
  serverUrl?: string;
  apiKey?: string;
}

type BridgeCommand =
  | SendCommand
  | SendMediaCommand
  | SendReactionCommand
  | StartTypingCommand
  | EditMessageCommand
  | ConfigCommand;

interface BridgeMessage {
  type: 'message' | 'status' | 'error';
  [key: string]: unknown;
}

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private client: IMessageClient | null = null;
  private clients: Set<WebSocket> = new Set();
  private config: BridgeConfig;

  constructor(private port: number, config: BridgeConfig) {
    this.config = config;
  }

  async start(): Promise<void> {
    this.wss = new WebSocketServer({ host: '127.0.0.1', port: this.port });
    console.log(`🌉 Bridge server listening on ws://127.0.0.1:${this.port}`);

    this.wss.on('connection', (ws) => {
      console.log('🔗 Python client connected');
      this.setupClient(ws);
    });

    await this.initIMessageClient();
  }

  private async initIMessageClient(): Promise<void> {
    const onMessage = (msg: InboundMessage) => this.broadcast({ type: 'message', ...msg });
    const onStatus = (status: string) => this.broadcast({ type: 'status', status });

    if (this.config.local) {
      this.client = new LocalClient({ onMessage, onStatus });
    } else {
      if (!this.config.serverUrl || !this.config.apiKey) {
        console.error('Remote mode requires IMESSAGE_SERVER_URL and IMESSAGE_API_KEY');
        this.broadcast({ type: 'error', error: 'Missing serverUrl or apiKey for remote mode' });
        return;
      }
      this.client = new RemoteClient({
        serverUrl: this.config.serverUrl,
        apiKey: this.config.apiKey,
        onMessage,
        onStatus,
      });
    }

    await this.client.connect();
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);

    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as BridgeCommand;

        if (cmd.type === 'config') {
          await this.handleConfigUpdate(cmd);
          ws.send(JSON.stringify({ type: 'status', status: 'configured' }));
          return;
        }

        await this.handleCommand(cmd);
        if ('to' in cmd) {
          ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
        }
      } catch (error) {
        console.error('Error handling command:', error);
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    ws.on('close', () => {
      console.log('🔌 Python client disconnected');
      this.clients.delete(ws);
    });

    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.clients.delete(ws);
    });
  }

  private async handleConfigUpdate(cmd: ConfigCommand): Promise<void> {
    const needsRestart =
      cmd.local !== this.config.local ||
      cmd.serverUrl !== this.config.serverUrl ||
      cmd.apiKey !== this.config.apiKey;

    if (!needsRestart) return;

    this.config = {
      local: cmd.local,
      serverUrl: cmd.serverUrl || '',
      apiKey: cmd.apiKey || '',
    };

    if (this.client) {
      await this.client.disconnect();
      this.client = null;
    }

    console.log(`Switching to ${cmd.local ? 'local' : 'remote'} mode...`);
    await this.initIMessageClient();
  }

  private async handleCommand(cmd: BridgeCommand): Promise<void> {
    if (!this.client) return;

    switch (cmd.type) {
      case 'send':
        await this.client.sendMessage(cmd.to, cmd.text);
        break;
      case 'send_media':
        await this.client.sendMedia(cmd.to, cmd.filePath, cmd.mimetype, cmd.caption, cmd.fileName);
        break;
      case 'send_reaction':
        if (this.client.sendReaction) {
          await this.client.sendReaction(cmd.chatGuid, cmd.messageGuid, cmd.reaction);
        }
        break;
      case 'start_typing':
        if (this.client.startTyping) {
          await this.client.startTyping(cmd.chatGuid);
        }
        break;
      case 'edit_message':
        if (this.client.editMessage) {
          await this.client.editMessage(cmd.messageGuid, cmd.text);
        }
        break;
    }
  }

  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  async stop(): Promise<void> {
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    if (this.client) {
      await this.client.disconnect();
      this.client = null;
    }
  }
}
