const $ = (id) => document.getElementById(id);

const els = {
  healthDot: $("healthDot"),
  healthText: $("healthText"),
  workspaceSelect: $("workspaceSelect"),
  workspaceHint: $("workspaceHint"),
  refreshBtn: $("refreshBtn"),
  createForm: $("createForm"),
  newWorkspaceName: $("newWorkspaceName"),
  dropZone: $("dropZone"),
  fileInput: $("fileInput"),
  uploadState: $("uploadState"),
  messages: $("messages"),
  emptyState: $("emptyState"),
  chatForm: $("chatForm"),
  chatInput: $("chatInput"),
  sendBtn: $("sendBtn"),
  chatError: $("chatError"),
  modeTag: $("modeTag"),
};

let streaming = false;

function setHealth(kind, text) {
  els.healthDot.className = "dot " + (kind || "");
  els.healthText.textContent = text;
}

function setUploadState(kind, text) {
  els.uploadState.hidden = false;
  els.uploadState.className = "state " + (kind || "");
  els.uploadState.textContent = text;
}

function showError(message) {
  els.chatError.hidden = false;
  els.chatError.textContent = message;
}

function clearError() {
  els.chatError.hidden = true;
  els.chatError.textContent = "";
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.message || payload.detail || `请求失败（${response.status}）`);
  }
  return payload;
}

async function checkHealth() {
  try {
    const payload = await readJson(await fetch("/api/health"));
    setHealth("ok", `已连接 · ${payload.base_url}`);
    return true;
  } catch (error) {
    setHealth("err", "服务不可用");
    setUploadState("err", error.message);
    return false;
  }
}

async function loadWorkspaces(preselect) {
  els.workspaceSelect.disabled = true;
  try {
    const payload = await readJson(await fetch("/api/workspaces"));
    const items = payload.data || [];
    els.workspaceSelect.innerHTML = "";
    if (!items.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无工作区，请先创建";
      els.workspaceSelect.appendChild(option);
      els.workspaceHint.textContent = "还没有工作区，创建后即可上传文档。";
      return;
    }
    items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.slug;
      option.textContent = `${item.name}（${item.documents} 篇文档）`;
      els.workspaceSelect.appendChild(option);
    });
    if (preselect) els.workspaceSelect.value = preselect;
    els.workspaceHint.textContent = "切换工作区会同时切换文档范围与对话上下文。";
  } catch (error) {
    setHealth("err", "工作区读取失败");
    setUploadState("err", error.message);
  } finally {
    els.workspaceSelect.disabled = false;
  }
}

function appendBubble(role, text) {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  els.messages.appendChild(bubble);
  els.messages.scrollTop = els.messages.scrollHeight;
  return bubble;
}

async function sendMessage(event) {
  event.preventDefault();
  if (streaming) return;
  const message = els.chatInput.value.trim();
  const workspace = els.workspaceSelect.value;
  if (!message) return;
  if (!workspace) {
    showError("请先选择或创建一个工作区。");
    return;
  }

  clearError();
  els.chatInput.value = "";
  els.chatInput.style.height = "auto";
  appendBubble("user", message);
  const reply = appendBubble("bot pending", "正在生成回答…");
  els.sendBtn.disabled = true;
  streaming = true;

  let received = "";
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace, message }),
    });
    if (!response.ok || !response.body) {
      throw new Error(`流式接口不可用（${response.status}）`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";
      for (const block of events) {
        const line = block.split("\n").find((item) => item.startsWith("data:"));
        if (!line) continue;
        const raw = line.slice(5).trim();
        if (raw === "[DONE]") continue;
        const chunk = JSON.parse(raw);
        if (chunk.error) throw new Error(chunk.error);
        if (chunk.text) {
          received += chunk.text;
          reply.classList.remove("pending");
          reply.textContent = received;
          els.messages.scrollTop = els.messages.scrollHeight;
        }
      }
    }
  } catch (error) {
    // 流式失败时退回一次性接口，保证主流程仍能走通
    try {
      const payload = await readJson(
        await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace, message }),
        })
      );
      received = payload.data.text || "（空回答）";
      reply.classList.remove("pending");
      reply.textContent = received;
      if (payload.data.sources && payload.data.sources.length) {
        const sources = document.createElement("div");
        sources.className = "sources";
        payload.data.sources.slice(0, 4).forEach((source) => {
          const tag = document.createElement("span");
          tag.textContent = source.title || source.url || "文档";
          sources.appendChild(tag);
        });
        reply.appendChild(sources);
      }
    } catch (fallbackError) {
      reply.remove();
      showError(fallbackError.message || error.message);
    }
  } finally {
    els.sendBtn.disabled = false;
    streaming = false;
    if (!received) reply.textContent = "（未返回内容）";
  }
}

async function uploadFile(file) {
  const workspace = els.workspaceSelect.value;
  if (!workspace) {
    setUploadState("err", "请先选择工作区。");
    return;
  }
  if (!file) return;

  const form = new FormData();
  form.append("workspace", workspace);
  form.append("file", file);

  setUploadState("", `正在上传《${file.name}》…`);
  try {
    // 用 XHR 拿上传进度，fetch 目前没有上传进度事件
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100);
          setUploadState("", `正在上传《${file.name}》 ${percent}%`);
        }
      });
      xhr.onload = () => {
        let payload = {};
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (error) {
          payload = {};
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
        } else {
          reject(new Error(payload.message || payload.detail || `上传失败（${xhr.status}）`));
        }
      };
      xhr.onerror = () => reject(new Error("网络中断，上传失败。"));
      xhr.send(form);
    });
    setUploadState("ok", result.data.message);
    await loadWorkspaces(workspace);
  } catch (error) {
    setUploadState("err", error.message);
  } finally {
    els.fileInput.value = "";
  }
}

els.createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.newWorkspaceName.value.trim();
  if (!name) return;
  try {
    const payload = await readJson(
      await fetch("/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      })
    );
    els.newWorkspaceName.value = "";
    await loadWorkspaces(payload.data.slug);
    setUploadState("ok", `工作区《${payload.data.name}》已创建。`);
  } catch (error) {
    setUploadState("err", error.message);
  }
});

els.refreshBtn.addEventListener("click", () => loadWorkspaces(els.workspaceSelect.value));

els.dropZone.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", (event) => uploadFile(event.target.files[0]));

["dragenter", "dragover"].forEach((name) => {
  els.dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    els.dropZone.classList.add("dragover");
  });
});

["dragleave", "drop"].forEach((name) => {
  els.dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    els.dropZone.classList.remove("dragover");
  });
});

els.dropZone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  uploadFile(file);
});

els.chatForm.addEventListener("submit", sendMessage);

els.chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});

els.chatInput.addEventListener("input", () => {
  els.chatInput.style.height = "auto";
  els.chatInput.style.height = `${Math.min(els.chatInput.scrollHeight, 140)}px`;
});

(async function init() {
  const healthy = await checkHealth();
  if (healthy) await loadWorkspaces();
})();
