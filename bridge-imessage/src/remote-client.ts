/**
 * Remote iMessage client using @photon-ai/advanced-imessage-kit.
 *
 * Connects to a Photon-managed iMessage server over HTTP + Socket.IO.
 * Supports the full feature set: reactions, typing indicators, message
 * editing, polls, and file attachments.
 */

import { SDK } from '@photon-ai/advanced-imessage-kit';
import { readFile } from 'fs/promises';
import { basename } from 'path';
import type { ClientCallbacks, IMessageClient, InboundMessage } from './types.js';

interface RemoteClientOptions extends ClientCallbacks {
  serverUrl: string;
  apiKey: string;
}

export class RemoteClient implements IMessageClient {
  private sdk: ReturnType<typeof SDK> | null = null;
  private options: RemoteClientOptions;

  constructor(options: RemoteClientOptions) {
    this.options = options;
  }

  async connect(): Promise<void> {
    this.sdk = SDK({
      serverUrl: this.options.serverUrl,
      apiKey: this.options.apiKey,
      logLevel: 'info',
    });

    await this.sdk.connect();

    await new Promise<void>((resolve) => {
      this.sdk!.on('ready', () => {
        console.log('✅ Connected to Photon iMessage server');
        this.options.onStatus('ready');
        resolve();
      });
    });

    this.sdk.on('new-message', (messageResponse: any) => {
      if (messageResponse.isFromMe) return;

      const chatGuid: string =
        messageResponse.chats?.[0]?.guid ?? messageResponse.chatGuid ?? '';
      const sender: string = messageResponse.handle?.address ?? '';
      const isGroup = chatGuid.includes(';+;');

      const attachmentPaths: string[] = [];
      if (messageResponse.attachments?.length) {
        for (const att of messageResponse.attachments) {
          if (att.transferName) attachmentPaths.push(att.transferName);
        }
      }

      const msg: InboundMessage = {
        id: messageResponse.guid,
        sender,
        chatId: chatGuid,
        content: messageResponse.text || '',
        timestamp: messageResponse.dateCreated ?? Date.now(),
        isGroup,
        isFromMe: false,
        source: 'remote',
        ...(attachmentPaths.length > 0 ? { media: attachmentPaths } : {}),
      };

      this.options.onMessage(msg);
    });

    this.sdk.on('disconnect', () => {
      this.options.onStatus('disconnected');
    });

    this.sdk.on('error', (error: any) => {
      console.error('Remote SDK error:', error);
    });
  }

  async disconnect(): Promise<void> {
    if (this.sdk) {
      await this.sdk.close();
      this.sdk = null;
    }
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    await this.sdk.messages.sendMessage({
      chatGuid: to,
      message: text,
    });
  }

  async sendMedia(
    to: string,
    filePath: string,
    _mimetype: string,
    caption?: string,
    fileName?: string,
  ): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    await this.sdk.attachments.sendAttachment({
      chatGuid: to,
      filePath,
      fileName: fileName || basename(filePath),
    });

    if (caption) {
      await this.sdk.messages.sendMessage({
        chatGuid: to,
        message: caption,
      });
    }
  }

  async sendReaction(chatGuid: string, messageGuid: string, reaction: string): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    await this.sdk.messages.sendReaction({
      chatGuid,
      messageGuid,
      reaction: reaction as any,
    });
  }

  async startTyping(chatGuid: string): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    await this.sdk.chats.startTyping(chatGuid);
  }

  async editMessage(messageGuid: string, text: string): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    await this.sdk.messages.editMessage({
      messageGuid,
      editedMessage: text,
    });
  }
}
