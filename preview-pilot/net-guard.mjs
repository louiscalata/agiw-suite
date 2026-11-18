// In-process network scope for the pilot programs. Node's --allow-net (v25+) is all-or-nothing and
// ignores host/port values, so every outbound socket is checked here: only the configured
// loopback model endpoint (literal 127.0.0.1 and its exact port) may be reached. Fail closed.
import net from 'node:net';

let installed = null;

function target(args) {
  const first = args[0];
  const options = Array.isArray(first) ? first[0] : first;
  if (options !== null && typeof options === 'object') {
    return {path: options.path ?? undefined, host: options.host ?? 'localhost', port: options.port};  // http.Agent passes path: null
  }
  return {path: typeof first === 'string' && !/^\d+$/.test(first) ? first : undefined,
    host: typeof args[1] === 'string' ? args[1] : 'localhost', port: first};
}

export function installNetGuard(endpoint) {
  const u = new URL(endpoint);
  const port = Number(u.port);
  if (u.protocol !== 'http:' || u.hostname !== '127.0.0.1' || !Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw Error('NET_GUARD_ENDPOINT_REFUSED');
  }
  if (installed !== null) {
    if (installed !== port) throw Error('NET_GUARD_ALREADY_INSTALLED');
    return port;
  }
  const original = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function guardedConnect(...args) {
    const t = target(args);
    if (t.path !== undefined || t.host !== '127.0.0.1' || Number(t.port) !== port) {
      const error = Object.assign(new Error('NET_GUARD_REFUSED'), {code: 'NET_GUARD_REFUSED'});
      process.nextTick(() => this.destroy(error));
      return this;
    }
    return original.apply(this, args);
  };
  installed = port;
  return port;
}
