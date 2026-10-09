// Local-only upstream issue repro. Never modifies the supplied runtime package.
import { createServer } from 'node:https';
import { connect } from 'node:net';
import { connect as tlsConnect } from 'node:tls';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

if (!process.argv[2]) throw new Error('Usage: node srt-connect-tail.mjs <runtime-package-dir>');
const base = pathToFileURL(resolve(process.argv[2], 'dist/sandbox') + '/');
async function load(name, patched = false) {
  const url = new URL(name, base);
  let source = await readFile(url, 'utf8');
  if (patched) {
    const before = name === 'http-proxy.js'
      ? "upstream.on('close', () => socket.destroy());"
      : "upstream.once('close', () => client.destroy());";
    if (source.split(before).length !== 2) throw new Error(`Expected exactly one close handler in ${name}`);
    source = source.replace(before, before.replace('destroy()', 'end()'));
  }
  source = source.replaceAll(/from '([.][^']+)'/g, (_, path) => `from '${new URL(path, url).href}'`);
  return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}
const muxModule = await load('mux-proxy.js');
const proxyPatched = await load('http-proxy.js', true);
const variants = [
  ['original', await load('http-proxy.js'), muxModule, 3],
  ['connect-only', proxyPatched, muxModule, 10],
  ['both-drain', proxyPatched, await load('mux-proxy.js', true), 30],
];
const scratch = await mkdtemp(join(tmpdir(), 'srt-tail-'));
const key = join(scratch, 'key.pem');
const cert = join(scratch, 'cert.pem');
let origin;
try {
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-keyout', key, '-out', cert, '-subj', '/CN=localhost'], { stdio: 'ignore' });
  const payload = Buffer.alloc(3864935, 97);
  const digest = createHash('sha256').update(payload).digest('hex');
  origin = createServer({ key: await readFile(key), cert: await readFile(cert) }, (_, res) => {
    res.writeHead(200, { 'Content-Length': payload.length, Connection: 'close' });
    res.end(payload);
  });
  await new Promise(resolveListen => origin.listen(0, '127.0.0.1', resolveListen));
  for (const [label, proxyModule, muxVariant, rounds] of variants) {
    for (let round = 0; round < rounds; round++) {
      const proxy = proxyModule.createHttpProxyServer({ filter: async () => true });
      const mux = muxVariant.createMuxProxyServer({ httpServer: proxy, handleSocksConnection: socket => socket.destroy() });
      let client;
      let tls;
      try {
        await mux.listenHttpBackend();
        await new Promise(resolveListen => mux.server.listen(0, '127.0.0.1', resolveListen));
        client = connect(mux.getPort(), '127.0.0.1');
        await new Promise((resolveConnect, reject) => {
          let response = '';
          const onData = chunk => {
            response += chunk.toString();
            if (!response.includes('\r\n\r\n')) return;
            client.removeListener('data', onData);
            client.removeListener('error', reject);
            if (response.startsWith('HTTP/1.1 200')) resolveConnect();
            else reject(new Error(response));
          };
          client.setTimeout(10000, () => client.destroy(new Error('CONNECT timeout')));
          client.once('error', reject);
          client.on('data', onData);
          client.once('connect', () => client.write(`CONNECT 127.0.0.1:${origin.address().port} HTTP/1.1\r\nHost: localhost\r\n\r\n`));
        });
        const chunks = [];
        // The only TLS peer is this script's loopback origin with an ephemeral certificate.
        tls = tlsConnect({ socket: client, rejectUnauthorized: false });
        await new Promise((resolveEnd, reject) => {
          tls.setTimeout(10000, () => tls.destroy(new Error('TLS timeout')));
          tls.once('secureConnect', () => tls.write('GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'));
          tls.on('data', chunk => { chunks.push(chunk); tls.pause(); setTimeout(() => tls.resume(), 10); });
          tls.once('error', reject);
          tls.once('end', resolveEnd);
        });
        const all = Buffer.concat(chunks);
        const body = all.subarray(all.indexOf('\r\n\r\n') + 4);
        const match = createHash('sha256').update(body).digest('hex') === digest;
        console.log(JSON.stringify({ label, round, expected: payload.length, bytes: body.length,
          match }));
        if (label === 'both-drain' && (body.length !== payload.length || !match)) process.exitCode = 1;
      } finally {
        tls?.destroy();
        client?.destroy();
        await mux.close();
      }
    }
  }
} finally {
  if (origin) {
    origin.closeAllConnections();
    await new Promise(resolveClose => origin.close(resolveClose));
  }
  await rm(scratch, { recursive: true, force: true });
}
