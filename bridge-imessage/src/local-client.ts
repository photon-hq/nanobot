/**
 * Local iMessage client using @photon-ai/imessage-kit.
 *
 * Requires macOS with Full Disk Access granted.  Reads from the local
 * iMessage database and sends via AppleScript.
 */

import { IMessageSDK } from '@photon-ai/imessage-kit';
import { readFile } from 'fs/promises';
import { basename } from 'path';
import type { ClientCallbacks, IMessageClient, InboundMessage } from './types.js';

export class LocalClient implements IMessageClient {
  private sdk: IMessageSDK | null = null;
  private callbacks: ClientCallbacks;

  constructor(callbacks: ClientCallbacks) {
    this.callbacks = callbacks;
  }

  async connect(): Promise<void> {
    if (process.platform !== 'darwin') {
      throw new Error('Local iMessage mode requires macOS');
    }

    this.sdk = new IMessageSDK();

    await this.sdk.startWatching({
      onMessage: (message) => {
        if (message.isFromMe) return;

        const attachmentPaths: string[] = [];
        if (message.attachments?.length) {
          for (const att of message.attachments) {
            if (att.filename) attachmentPaths.push(att.filename);
          }
        }

        const msg: InboundMessage = {
          id: message.guid,
          sender: message.sender,
          chatId: message.chatId,
          content: message.text || '',
          timestamp: message.date.getTime(),
          isGroup: message.isGroupChat,
          isFromMe: false,
          source: 'local',
          ...(attachmentPaths.length > 0 ? { media: attachmentPaths } : {}),
        };

        this.callbacks.onMessage(msg);
      },

      onError: (error) => {
        console.error('Local watcher error:', error);
      },
    });

    this.callbacks.onStatus('connected');
    console.log('✅ Local iMessage watcher started');
  }

  async disconnect(): Promise<void> {
    if (this.sdk) {
      this.sdk.stopWatching();
      await this.sdk.close();
      this.sdk = null;
    }
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');
    await this.sdk.send(to, text);
  }

  async sendMedia(
    to: string,
    filePath: string,
    _mimetype: string,
    _caption?: string,
    _fileName?: string,
  ): Promise<void> {
    if (!this.sdk) throw new Error('Not connected');

    const mime = _mimetype || 'application/octet-stream';
    if (mime.startsWith('image/')) {
      await this.sdk.send(to, { images: [filePath] });
    } else {
      await this.sdk.sendFile(to, filePath);
    }
  }
}
