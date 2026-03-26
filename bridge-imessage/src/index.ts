#!/usr/bin/env node
/**
 * nanobot iMessage Bridge
 *
 * Dual-mode bridge that connects iMessage to nanobot's Python backend
 * via WebSocket.
 *
 * - Local mode:  uses @photon-ai/imessage-kit (macOS, on-device)
 * - Remote mode: uses @photon-ai/advanced-imessage-kit (Photon cloud)
 *
 * The mode is selected at runtime by the first "config" message from
 * the Python channel, or by the IMESSAGE_LOCAL env variable.
 */

import { BridgeServer } from './server.js';

const PORT = parseInt(process.env.BRIDGE_PORT || '3002', 10);
const LOCAL = process.env.IMESSAGE_LOCAL !== 'false';
const SERVER_URL = process.env.IMESSAGE_SERVER_URL || '';
const API_KEY = process.env.IMESSAGE_API_KEY || '';

console.log('🐈 nanobot iMessage Bridge');
console.log('=========================\n');
console.log(`Mode: ${LOCAL ? 'local (imessage-kit)' : 'remote (advanced-imessage-kit)'}`);

const server = new BridgeServer(PORT, { local: LOCAL, serverUrl: SERVER_URL, apiKey: API_KEY });

process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

server.start().catch((error) => {
  console.error('Failed to start iMessage bridge:', error);
  process.exit(1);
});
