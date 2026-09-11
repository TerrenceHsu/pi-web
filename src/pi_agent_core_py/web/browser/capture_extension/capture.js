(() => {
  'use strict';
  const MIME = 'video/webm;codecs=vp8,opus';
  const MAX_CHUNK = 2 * 1024 * 1024;
  const MAX_QUEUE = 8 * 1024 * 1024;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function bounded(promise, milliseconds) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('timeout')), milliseconds);
      })]);
    } finally {
      clearTimeout(timer);
    }
  }
  let binding = null;
  let active = null;

  async function stop(record) {
    if (!record) return;
    if (record.stopping) return record.stopping;
    record.stopped = true;
    record.stopping = (async () => {
      // Restore tab muting before releasing Chromium's capture-local suppression.
      try {
        await chrome.tabs.update(record.tabId, {muted: true});
      } finally {
        try {
          if (record.recorder?.state !== 'inactive') record.recorder?.stop();
        } finally {
          record.stream?.getTracks().forEach(track => track.stop());
          record.queue.length = 0;
          record.prefix.length = 0;
          if (active === record) active = null;
        }
      }
    })();
    return record.stopping;
  }

  async function fail(record, code) {
    if (record.failed || record.stopped) return;
    record.failed = true;
    const stopping = stop(record).catch(() => {});
    try {
      await bounded(globalThis[binding]({kind: 'error', generation: record.generation,
        token: record.token, code}), 10000);
    } catch (_) { /* The owner disappeared; cleanup still runs. */ }
    await stopping;
  }

  async function pump(record) {
    if (record.pumping) return;
    record.pumping = true;
    try {
      while (!record.stopped && record.queue.length) {
        const blob = record.queue.shift();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 16384) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
        }
        const accepted = await bounded(globalThis[binding]({kind: 'chunk',
          generation: record.generation, token: record.token,
          seq: record.seq++, data: btoa(binary)}), 10000);
        record.queuedBytes -= blob.size;
        if (accepted !== true) throw new Error('delivery');
      }
    } catch (_) {
      await fail(record, 'browser_media_stream_failed');
    } finally {
      record.pumping = false;
    }
  }

  async function start(options) {
    if (active) throw new Error('browser_media_busy');
    if (!binding || typeof globalThis[binding] !== 'function' ||
        !navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder ||
        !MediaRecorder.isTypeSupported(MIME)) throw new Error('browser_media_codec_unsupported');
    const record = {tabId: options.tabId, token: options.token,
      generation: options.generation, seq: 1, queuedBytes: 0, queue: [],
      prefix: [], prefixBytes: 0, prefixReady: false,
      stopped: false, failed: false, pumping: false, stopping: null,
      stream: null, recorder: null};
    active = record;
    try {
      await chrome.tabs.update(record.tabId, {muted: true});
      const id = await chrome.tabCapture.getMediaStreamId({targetTabId: record.tabId});
      record.stream = await navigator.mediaDevices.getUserMedia({
        audio: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: id}},
        video: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: id,
          minWidth: 1920, maxWidth: 1920, minHeight: 1080, maxHeight: 1080,
          // Keep native refresh frames arriving even when the tab is static.
          // Sparse one-frame WebM clusters otherwise leave undecodable time gaps.
          // This is a capture hint; the UI still measures actual presented frames.
          minFrameRate: 30, maxFrameRate: 30}},
      });
      if (record.stopped) {
        record.stream.getTracks().forEach(track => track.stop());
        throw new Error('browser_media_capture_failed');
      }
      const audio = record.stream.getAudioTracks();
      const video = record.stream.getVideoTracks();
      if (audio.length !== 1 || video.length !== 1) throw new Error('browser_media_codec_unsupported');
      // Chrome's tab source encodes this in its device identifier. Do not send
      // that identifier, track labels, or stream IDs outside this private helper.
      const localSuppressed = /\blocal_echo=false\b/.test(String(audio[0].getSettings().deviceId || ''));
      if (!localSuppressed) throw new Error('browser_media_capture_failed');
      let captureActive = false;
      for (let attempt = 0; attempt < 40 && !record.stopped; attempt++) {
        const captures = await chrome.tabCapture.getCapturedTabs();
        if (captures.some(item => item.tabId === record.tabId && item.status === 'active')) {
          captureActive = true;
          break;
        }
        await delay(25);
      }
      if (!captureActive || record.stopped) throw new Error('browser_media_capture_failed');
      record.recorder = new MediaRecorder(record.stream, {
        mimeType: MIME, videoBitsPerSecond: 4000000, audioBitsPerSecond: 96000,
        // Request random-access frames by elapsed time, not frame count: static
        // tabs emit far fewer frames and must remain safe for bounded MSE pruning.
        // This requests a keyframe on the next arriving frame, not synthetic FPS.
        videoKeyFrameIntervalDuration: 1000,
      });
      record.recorder.ondataavailable = event => {
        if (record.stopped || !event.data.size) return;
        if (event.data.size > MAX_CHUNK || record.queuedBytes + event.data.size > MAX_QUEUE) {
          void fail(record, 'browser_media_stream_failed');
          return;
        }
        record.queuedBytes += event.data.size;
        if (!record.prefixReady) {
          // MediaRecorder may emit even the four-byte EBML signature in separate
          // events. Keep every byte, but make the first transport packet testable.
          record.prefix.push(event.data);
          record.prefixBytes += event.data.size;
          if (record.prefixBytes < 4) return;
          const initial = new Blob(record.prefix);
          record.prefix.length = 0;
          record.prefixBytes = 0;
          record.prefixReady = true;
          // A three-byte prefix plus a maximum-sized next event must not create
          // an oversized packet. Blob slices preserve order without copying it.
          for (let offset = 0; offset < initial.size; offset += MAX_CHUNK) {
            record.queue.push(initial.slice(offset, offset + MAX_CHUNK));
          }
        } else {
          record.queue.push(event.data);
        }
        void pump(record);
      };
      record.recorder.onerror = () => void fail(record, 'browser_media_capture_failed');
      record.stream.getTracks().forEach(track => {
        track.onended = () => void fail(record, 'browser_media_capture_failed');
      });
      const settings = video[0].getSettings();
      await chrome.tabs.update(record.tabId, {muted: false});
      if (record.stopped) {
        await chrome.tabs.update(record.tabId, {muted: true});
        throw new Error('browser_media_capture_failed');
      }
      record.recorder.start(100);
      return {mime: MIME, width: settings.width, height: settings.height,
        frameRate: settings.frameRate, audioSafe: localSuppressed};
    } catch (error) {
      await stop(record).catch(() => {});
      const code = String(error?.message || '');
      throw new Error(['browser_media_codec_unsupported', 'browser_media_capture_failed'].includes(code)
        ? code : 'browser_media_capture_failed');
    }
  }

  const api = {
    async call(options) {
      switch (options.command) {
        case 'init':
          if (binding || typeof options.binding !== 'string' ||
              typeof globalThis[options.binding] !== 'function') throw new Error('browser_media_codec_unsupported');
          binding = options.binding;
          return {incognito: chrome.extension.inIncognitoContext,
            supported: !!chrome.tabCapture && !!chrome.tabs && !!chrome.action &&
              !!globalThis.MediaRecorder && MediaRecorder.isTypeSupported(MIME)};
        case 'arm':
          return chrome.runtime.sendMessage({kind: 'arm', token: options.token, expected: options.expected});
        case 'take':
          return chrome.runtime.sendMessage({kind: 'take', token: options.token});
        case 'cancel':
          return chrome.runtime.sendMessage({kind: 'cancel', token: options.token});
        case 'mute':
          await chrome.tabs.update(options.tabId, {muted: true});
          return true;
        case 'start':
          return start(options);
        case 'stop':
          if (active?.token === options.token) await stop(active);
          return true;
        default:
          throw new Error('browser_media_codec_unsupported');
      }
    },
  };
  Object.defineProperty(globalThis, 'piCapture', {value: Object.freeze(api)});
  addEventListener('pagehide', () => { if (active) void stop(active).catch(() => {}); });
})();
