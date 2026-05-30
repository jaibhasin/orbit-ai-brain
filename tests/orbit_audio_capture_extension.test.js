"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionDir = path.join(__dirname, "..", "extension", "orbit-audio-capture");

async function testToolbarActivationReportsStatus() {
  const runtimeListeners = [];
  const actionListeners = [];
  const commandListeners = [];
  const sentTabMessages = [];

  const context = {
    chrome: {
      action: {
        onClicked: {
          addListener(listener) {
            actionListeners.push(listener);
          }
        }
      },
      commands: {
        onCommand: {
          addListener(listener) {
            commandListeners.push(listener);
          }
        }
      },
      offscreen: {
        async createDocument() {}
      },
      runtime: {
        lastError: null,
        onMessage: {
          addListener(listener) {
            runtimeListeners.push(listener);
          }
        },
        async getContexts() {
          return [];
        },
        getURL(filePath) {
          return `chrome-extension://orbit/${filePath}`;
        },
        async sendMessage(message) {
          assert.equal(message.type, "ORBIT_OFFSCREEN_START");
          return { ok: true };
        }
      },
      storage: {
        session: {
          async set() {},
          async get() {
            return {};
          }
        }
      },
      tabCapture: {
        getMediaStreamId(_options, callback) {
          callback("stream-id");
        }
      },
      tabs: {
        async query() {
          return [{ id: 7 }];
        },
        async sendMessage(tabId, message) {
          sentTabMessages.push({ tabId, message });
        }
      }
    },
    console
  };

  vm.runInNewContext(
    fs.readFileSync(path.join(extensionDir, "service_worker.js"), "utf8"),
    context
  );

  await new Promise((resolve, reject) => {
    const handled = runtimeListeners[0](
      {
        type: "ORBIT_CAPTURE_CONFIG",
        sessionId: "session-1",
        meetingId: "abc-defg-hij",
        webSocketUrl: "ws://127.0.0.1:8000/internal/audio-stream/session-1"
      },
      { tab: { id: 7 } },
      (response) => {
        try {
          assert.equal(response.ok, true);
          assert.equal(response.cached, true);
          resolve();
        } catch (error) {
          reject(error);
        }
      }
    );
    assert.equal(handled, true);
  });

  await actionListeners[0]({ id: 7 });

  assert.equal(sentTabMessages.length, 1);
  assert.equal(sentTabMessages[0].tabId, 7);
  assert.equal(sentTabMessages[0].message.type, "ORBIT_CAPTURE_STATUS");
  assert.equal(sentTabMessages[0].message.ok, true);
  assert.equal(sentTabMessages[0].message.error, undefined);
}

function testContentScriptDisplaysActiveStatus() {
  const runtimeListeners = [];
  const button = {
    disabled: false,
    style: {},
    textContent: "Use Alt+Shift+O or the extension icon"
  };

  const context = {
    chrome: {
      runtime: {
        onMessage: {
          addListener(listener) {
            runtimeListeners.push(listener);
          }
        }
      }
    },
    console,
    document: {
      getElementById(id) {
        assert.equal(id, "orbit-audio-capture-button");
        return button;
      }
    },
    window: {
      addEventListener() {}
    }
  };

  vm.runInNewContext(
    fs.readFileSync(path.join(extensionDir, "content.js"), "utf8"),
    context
  );

  runtimeListeners[0]({ type: "ORBIT_CAPTURE_STATUS", ok: true });

  assert.equal(button.textContent, "Orbit audio active");
  assert.equal(button.disabled, true);
  assert.equal(button.style.opacity, "0.72");
}

async function main() {
  await testToolbarActivationReportsStatus();
  testContentScriptDisplaysActiveStatus();
  console.log("Orbit audio capture extension tests passed.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
