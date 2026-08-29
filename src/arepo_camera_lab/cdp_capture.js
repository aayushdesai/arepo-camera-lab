#!/usr/bin/env node

import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import {pathToFileURL} from 'node:url';

function argumentsMap(argv) {
  const result = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index].startsWith('--') || argv[index + 1] === undefined) {
      throw new Error(`invalid argument near ${argv[index] ?? 'end of command'}`);
    }
    result.set(argv[index].slice(2), argv[index + 1]);
  }
  return result;
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function waitForPage(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const pages = await response.json();
      const page = pages.find(entry => entry.type === 'page');
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch (_) {
      // Chrome has not opened its debugging endpoint yet.
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('timed out waiting for Chrome DevTools');
}

class DevTools {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, {once: true});
      this.socket.addEventListener('error', reject, {once: true});
    });
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(String(event.data));
      if (!message.id) return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
      else waiter.resolve(message.result ?? {});
    });
  }

  async call(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    const response = new Promise((resolve, reject) => {
      this.pending.set(id, {resolve, reject});
    });
    this.socket.send(JSON.stringify({id, method, params}));
    return response;
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(client, expression) {
  const result = await client.call('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: false,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description ??
      result.exceptionDetails.text ?? 'browser evaluation failed');
  }
  return result.result?.value;
}

async function waitForCaptureApi(client, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ready = await evaluate(client,
      "document.readyState === 'complete' && Boolean(window.AREPO_CAMERA_LAB_CAPTURE)");
    if (ready) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('timed out waiting for the WebGL capture API');
}

function digest(buffer) {
  return createHash('sha256').update(buffer).digest('hex');
}

async function main() {
  const args = argumentsMap(process.argv.slice(2));
  const html = path.resolve(args.get('html'));
  const planPath = path.resolve(args.get('plan'));
  const output = path.resolve(args.get('output'));
  const chrome = path.resolve(args.get('chrome'));
  const width = Number(args.get('width') ?? 1920);
  const height = Number(args.get('height') ?? 1080);
  const plan = JSON.parse(await readFile(planPath, 'utf8'));
  if (plan.schema !== 'arepo_camera_lab_capture_plan_v001' || !Array.isArray(plan.captures)) {
    throw new Error('capture plan schema must be arepo_camera_lab_capture_plan_v001');
  }
  await mkdir(output, {recursive: true});
  const port = await freePort();
  const userData = await mkdtemp(path.join(os.tmpdir(), 'arepo-camera-lab-chrome-'));
  const child = spawn(chrome, [
    '--headless=new', `--remote-debugging-port=${port}`,
    `--user-data-dir=${userData}`, '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--ignore-gpu-blocklist', '--enable-unsafe-swiftshader',
    '--use-angle=swiftshader', '--remote-allow-origins=*', 'about:blank',
  ], {stdio: ['ignore', 'ignore', 'pipe']});
  let chromeError = '';
  child.stderr.on('data', chunk => { chromeError += String(chunk); });
  let client;
  try {
    client = new DevTools(await waitForPage(port));
    await client.call('Page.enable');
    await client.call('Runtime.enable');
    await client.call('Emulation.setDeviceMetricsOverride', {
      width, height, deviceScaleFactor: 1, mobile: false,
    });
    await client.call('Page.navigate', {url: pathToFileURL(html).href});
    await waitForCaptureApi(client);
    const api = await evaluate(client,
      '({schema:window.AREPO_CAMERA_LAB_CAPTURE.schema,channels:window.AREPO_CAMERA_LAB_CAPTURE.channels,scene:window.AREPO_CAMERA_LAB_CAPTURE.scene})');
    const records = [];
    for (const capture of plan.captures) {
      const relative = String(capture.output);
      if (path.isAbsolute(relative) || relative.split(path.sep).includes('..')) {
        throw new Error(`capture output must stay below the output directory: ${relative}`);
      }
      const target = path.join(output, relative);
      await mkdir(path.dirname(target), {recursive: true});
      const state = await evaluate(client,
        `window.AREPO_CAMERA_LAB_CAPTURE.prepare(${JSON.stringify(capture.pose)},${JSON.stringify(capture.channel)},${JSON.stringify(capture.settings)},${JSON.stringify(capture.visible_scene_binding ?? null)})`);
      const screenshot = await client.call('Page.captureScreenshot', {
        format: 'png', fromSurface: true, captureBeyondViewport: false,
      });
      const image = Buffer.from(screenshot.data, 'base64');
      await writeFile(target, image, {flag: 'wx'});
      records.push({
        pose_index: capture.pose_index,
        pose_id: capture.pose.pose_id ?? null,
        snapshot: Number(capture.pose.snapshot),
        visible_scene_binding: capture.visible_scene_binding ?? null,
        channel: capture.channel,
        output: relative,
        sha256: digest(image),
        bytes: image.length,
        state,
      });
      process.stdout.write(`AREPO_CAMERA_LAB_CAPTURED ${records.length}/${plan.captures.length} ${relative}\n`);
    }
    const recordPath = path.join(output, String(plan.records_output));
    await writeFile(recordPath, JSON.stringify({
      schema: 'arepo_camera_lab_capture_records_v001',
      browser_api: api,
      width, height,
      captures: records,
    }, null, 2) + '\n', {flag: 'wx'});
  } finally {
    if (client) {
      try { await client.call('Browser.close'); } catch (_) {}
      client.close();
    }
    if (!child.killed) child.kill('SIGTERM');
    await new Promise(resolve => setTimeout(resolve, 200));
    await rm(userData, {recursive: true, force: true});
    if (child.exitCode && child.exitCode !== 0) {
      process.stderr.write(chromeError);
    }
  }
}

main().catch(error => {
  console.error(`arepo-camera-lab-capture: ${error.stack ?? error}`);
  process.exitCode = 1;
});
