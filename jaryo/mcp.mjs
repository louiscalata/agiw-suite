/** Stdio tool host for the kit. Logs stay on stderr. Stdout is framed JSON only. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { checkKit } from './contract.mjs';
import { installKit, isDirectRun, kitRoot } from './install.mjs';

const TOOLS = [
  {
    name: 'jaryo_tokens',
    description: 'Return the Jaryo token file, including the black and white theme contract and the scale.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'jaryo_install',
    description: 'Copy the Jaryo kit into a directory. Then run node <dest>/cli.mjs demo.',
    inputSchema: {
      type: 'object',
      properties: { dest: { type: 'string', description: 'Directory to create' } },
      required: ['dest'],
    },
  },
  {
    name: 'jaryo_check',
    description: 'Check the kit files for the token, state, motion, and pipeline contract.',
    inputSchema: { type: 'object', properties: {} },
  },
];

export function frameMessage(message) {
  const body = JSON.stringify(message);
  return `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`;
}

export function readFrames(buffer) {
  const messages = [];
  let rest = buffer;
  while (rest.length > 0) {
    const headerEnd = rest.indexOf('\r\n\r\n');
    if (headerEnd === -1) break;
    const header = rest.slice(0, headerEnd).toString('utf8');
    const match = /Content-Length:\s*(\d+)/i.exec(header);
    if (!match) throw new Error('Missing Content-Length');
    const length = Number(match[1]);
    const start = headerEnd + 4;
    if (rest.length < start + length) break;
    messages.push(JSON.parse(rest.slice(start, start + length).toString('utf8')));
    rest = rest.slice(start + length);
  }
  return { messages, rest };
}

function result(id, value) {
  return { jsonrpc: '2.0', id, result: value };
}

function failure(id, code, message) {
  return { jsonrpc: '2.0', id, error: { code, message } };
}

function toolText(text) {
  return { content: [{ type: 'text', text }] };
}

export function handleMessage(message) {
  const id = Object.prototype.hasOwnProperty.call(message, 'id') ? message.id : null;
  switch (message.method) {
    case 'initialize':
      return result(id, {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'jaryo', version: 'kit' },
      });
    case 'notifications/initialized':
      return null;
    case 'tools/list':
      return result(id, { tools: TOOLS });
    case 'tools/call': {
      const name = message.params && message.params.name;
      const args = message.params && message.params.arguments || {};
      switch (name) {
        case 'jaryo_tokens':
          return result(id, toolText(readFileSync(join(kitRoot(), 'tokens.json'), 'utf8')));
        case 'jaryo_install': {
          try {
            const dest = installKit(args.dest);
            return result(id, toolText(`Installed Jaryo kit in ${dest}\nRun: node ${dest}/cli.mjs demo --port 8731`));
          } catch (error) {
            return result(id, { content: [{ type: 'text', text: error.message }], isError: true });
          }
        }
        case 'jaryo_check': {
          const problems = checkKit(kitRoot());
          const text = problems.length === 0 ? 'ok' : problems.join('\n');
          return result(id, { content: [{ type: 'text', text }], isError: problems.length > 0 });
        }
        default:
          return failure(id, -32602, `Unknown tool: ${String(name)}`);
      }
    }
    default:
      if (id === null) return null;
      return failure(id, -32601, `Method not found: ${String(message.method)}`);
  }
}

export function serveMcp(input = process.stdin, output = process.stdout) {
  return new Promise((resolve) => {
    let buffer = Buffer.alloc(0);
    const write = (message) => {
      if (message) output.write(frameMessage(message));
    };
    input.on('data', (chunk) => {
      buffer = Buffer.concat([buffer, Buffer.from(chunk)]);
      try {
        const read = readFrames(buffer);
        buffer = read.rest;
        read.messages.forEach((message) => write(handleMessage(message)));
      } catch (error) {
        write(failure(null, -32700, error.message));
      }
    });
    input.on('end', () => resolve());
  });
}

if (isDirectRun(import.meta.url)) serveMcp();
