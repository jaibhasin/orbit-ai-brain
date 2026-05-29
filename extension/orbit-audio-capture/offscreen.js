let audioContext = null;
let mediaStream = null;
let processor = null;
let socket = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "ORBIT_OFFSCREEN_START") return false;

  startCapture(message)
    .then(() => sendResponse({ ok: true }))
    .catch((error) => {
      console.error("Orbit offscreen capture failed:", error);
      sendResponse({ ok: false, error: String(error) });
    });
  return true;
});

async function startCapture(config) {
  await stopCapture();

  const audioFormat = config.audioFormat || {};
  const sampleRate = Number(audioFormat.sampleRate || 16000);
  const channels = Number(audioFormat.channels || 1);

  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: config.streamId
      }
    },
    video: false
  });

  audioContext = new AudioContext({ sampleRate });
  const actualSampleRate = audioContext.sampleRate;

  socket = new WebSocket(config.webSocketUrl);
  socket.binaryType = "arraybuffer";
  await waitForSocketOpen(socket);
  socket.send(JSON.stringify({
    type: "start",
    encoding: "linear16",
    sample_rate: actualSampleRate,
    channels,
    session_id: config.sessionId,
    meeting_id: config.meetingId
  }));

  const source = audioContext.createMediaStreamSource(mediaStream);
  processor = audioContext.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (event) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const pcm16 = convertToMonoPcm16(event.inputBuffer);
    if (pcm16.byteLength > 0) socket.send(pcm16.buffer);
  };

  source.connect(processor);
  processor.connect(audioContext.destination);
}

async function stopCapture() {
  if (processor) {
    processor.disconnect();
    processor = null;
  }
  if (audioContext) {
    await audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    for (const track of mediaStream.getTracks()) track.stop();
    mediaStream = null;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "stop" }));
    socket.close();
  }
  socket = null;
}

function waitForSocketOpen(targetSocket) {
  return new Promise((resolve, reject) => {
    targetSocket.onopen = resolve;
    targetSocket.onerror = () => reject(new Error("Orbit audio WebSocket failed to open."));
  });
}

function convertToMonoPcm16(inputBuffer) {
  const length = inputBuffer.length;
  const channels = inputBuffer.numberOfChannels;
  const output = new Int16Array(length);

  for (let index = 0; index < length; index += 1) {
    let sample = 0;
    for (let channel = 0; channel < channels; channel += 1) {
      sample += inputBuffer.getChannelData(channel)[index];
    }
    sample /= Math.max(channels, 1);
    sample = Math.max(-1, Math.min(1, sample));
    output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }

  return output;
}
