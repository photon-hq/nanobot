export interface BridgeConfig {
  local: boolean;
  serverUrl: string;
  apiKey: string;
}

export interface InboundMessage {
  id: string;
  sender: string;
  chatId: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  isFromMe: boolean;
  source: 'local' | 'remote';
  media?: string[];
}

export interface ClientCallbacks {
  onMessage: (msg: InboundMessage) => void;
  onStatus: (status: string) => void;
}

export interface IMessageClient {
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  sendMessage(to: string, text: string): Promise<void>;
  sendMedia(
    to: string,
    filePath: string,
    mimetype: string,
    caption?: string,
    fileName?: string,
  ): Promise<void>;
  sendReaction?(chatGuid: string, messageGuid: string, reaction: string): Promise<void>;
  startTyping?(chatGuid: string): Promise<void>;
  editMessage?(messageGuid: string, text: string): Promise<void>;
}
