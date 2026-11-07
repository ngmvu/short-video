    (() => {
      const dashboardPages = [...document.querySelectorAll("[data-dashboard-page]")];
      const dashboardLinks = [...document.querySelectorAll("[data-dashboard-nav]")];

      function activateDashboardPage(pageId, updateHistory = false) {
        const selectedPage = dashboardPages.find((page) => page.dataset.dashboardPage === pageId)
          ? pageId
          : "workflow";
        dashboardPages.forEach((page) => {
          const isActive = page.dataset.dashboardPage === selectedPage;
          page.hidden = !isActive;
          page.classList.toggle("active", isActive);
        });
        dashboardLinks.forEach((link) => {
          const isActive = link.dataset.dashboardNav === selectedPage;
          link.classList.toggle("active", isActive);
          if (isActive) link.setAttribute("aria-current", "page");
          else link.removeAttribute("aria-current");
        });
        if (selectedPage === "workflow") {
          window.requestAnimationFrame(drawConnections);
        }
        if (updateHistory && window.location.hash !== `#${selectedPage}`) {
          window.history.pushState(null, "", `#${selectedPage}`);
        }
      }

      dashboardLinks.forEach((link) => {
        link.addEventListener("click", (event) => {
          event.preventDefault();
          activateDashboardPage(link.dataset.dashboardNav, true);
        });
      });
      window.addEventListener("popstate", () => {
        activateDashboardPage(window.location.hash.slice(1));
      });
      activateDashboardPage(window.location.hash.slice(1) || "workflow");

      const stage = document.getElementById("flow-stage");
      const svg = document.getElementById("connections");
      const message = document.getElementById("canvas-message");
      const statusPill = document.getElementById("process-status");
      const stopProcessButton = document.getElementById("stop-process-button");
      const openFlowLoginButton = document.getElementById("open-flow-login");
      const produceQueueTelegramBtn = document.getElementById("produce-queue-telegram-btn");
      const clearQueueBtn = document.getElementById("clear-queue-btn");
      const resendDraftsTelegramBtn = document.getElementById("resend-drafts-telegram-btn");
      const savedIdeasStatus = document.getElementById("saved-ideas-action-status");
      const workflowDraftsPill = document.getElementById("workflow-drafts-pill");
      const queueCountBadge = document.getElementById("queue-count-badge");
      const logOutput = document.getElementById("log-output");
      const logAutoscroll = document.getElementById("log-autoscroll");
      const refreshLogsBtn = document.getElementById("refresh-logs");
      const clearLogsBtn = document.getElementById("clear-logs-button");
      const logRunningHint = document.getElementById("log-running-hint");
      const workflowLogOutput = document.getElementById("workflow-log-output");
      const workflowLogAutoscroll = document.getElementById("workflow-log-autoscroll");
      const workflowRefreshLogsBtn = document.getElementById("workflow-refresh-logs");

      const storageKey = "short-video-distributor.workflow.v2";
      const definitions = {
        "generate-ideas": {
          title: "Tạo ý tưởng",
          icon: "✦",
          detail: "Sinh ý tưởng video và gửi Telegram để duyệt.",
          mode: "generate-ideas"
        },
        "telegram-listener": {
          title: "Telegram",
          icon: "✉",
          detail: "Lắng nghe phản hồi duyệt từ Telegram.",
          mode: "telegram-poll"
        },
        "process-queue": {
          title: "Sản xuất & đăng",
          icon: "▶",
          detail: "Tạo video từ hàng đợi đã duyệt và đăng lên nền tảng.",
          mode: "process-queue"
        },
        "approval-video": {
          title: "Duyệt → video → Telegram",
          icon: "✓",
          detail: "Chờ duyệt (Web/Telegram), tạo 3 Scene & ghép video rồi gửi qua Telegram.",
          mode: "approval-video"
        },
        "approval-publish": {
          title: "Duyệt → Video → Đăng MXH",
          icon: "🚀",
          detail: "Chờ duyệt (Web/Telegram), tạo 3 Scene & ghép video rồi tự đăng TikTok, FB Reels, YouTube Shorts.",
          mode: "approval-publish"
        },
        "automation": {
          title: "Tự động hóa (Schedule)",
          icon: "⟳",
          detail: "Lập lịch chạy flow tự động định kỳ.",
          mode: "auto"
        }
      };
      const defaultGraph = {
        nodes: [
          { id: "ideas", type: "generate-ideas", x: 90, y: 145 },
          { id: "approval-video", type: "approval-video", x: 435, y: 145 }
        ],
        edges: [
          { id: "ideas-approval-video", from: "ideas", to: "approval-video" }
        ]
      };
      let graph = loadGraph();
      let selectedNode = null;
      let selectedEdge = null;
      let connectingFrom = null;
      let runningNode = null;
      let processMode = null;
      let workflowProgress = null;
      let currentQueueCount = Number(queueCountBadge?.textContent || 0);
      let currentDraftCount = document.querySelectorAll("#workflow-saved-drafts-list .saved-draft-item").length;

      function loadGraph() {
        try {
          const saved = JSON.parse(localStorage.getItem(storageKey));
          if (!saved || !Array.isArray(saved.nodes) || !Array.isArray(saved.edges)) {
            return structuredClone(defaultGraph);
          }
          const nodes = saved.nodes.filter((node) =>
            node && typeof node.id === "string" && definitions[node.type] &&
            Number.isFinite(node.x) && Number.isFinite(node.y)
          );
          const nodeIds = new Set(nodes.map((node) => node.id));
          const edges = saved.edges.filter((edge) =>
            edge && typeof edge.id === "string" && nodeIds.has(edge.from) &&
            nodeIds.has(edge.to) && edge.from !== edge.to
          );
          return { nodes, edges };
        } catch (error) {
          return structuredClone(defaultGraph);
        }
      }

      function saveGraph() {
        localStorage.setItem(storageKey, JSON.stringify(graph));
      }

      function tell(text, isError = false) {
        message.textContent = text;
        message.classList.toggle("error", isError);
      }

      function tellSavedIdeas(text, isError = false) {
        if (!savedIdeasStatus) return;
        savedIdeasStatus.textContent = text;
        savedIdeasStatus.className = isError ? "bad" : "ok";
      }

      async function safeParseJson(response, fallbackMsg = "Có lỗi xảy ra.") {
        const contentType = response.headers.get("content-type") || "";
        let data = null;
        if (contentType.includes("application/json")) {
          try {
            data = await response.json();
          } catch (e) {
            data = null;
          }
        }
        if (!data) {
          let text = "";
          try {
            text = await response.text();
          } catch (e) {
            text = "";
          }
          if (text.includes("CSRF") || text.includes("token")) {
            throw new Error("Phiên làm việc hoặc CSRF token đã hết hạn. Hãy bấm F5 để tải lại trang rồi thử lại.");
          }
          if (!response.ok) {
            throw new Error(`Máy chủ phản hồi mã lỗi ${response.status}. Hãy bấm F5 tải lại trang.`);
          }
          throw new Error(fallbackMsg);
        }
        if (!response.ok) {
          throw new Error(data.error || fallbackMsg);
        }
        return data;
      }

      function updateLogViewer(preEl, autoscrollEl, logText) {
        if (!preEl) return;
        const emptyText = preEl.dataset.emptyText || "Chưa có log nào được ghi.";
        const nextText = logText && logText.trim() ? logText : emptyText;
        if (preEl.textContent !== nextText) {
          preEl.textContent = nextText;
          if (!autoscrollEl || autoscrollEl.checked) {
            preEl.scrollTop = preEl.scrollHeight;
          }
        }
      }

      function updateLogs(logText, isRunning) {
        updateLogViewer(logOutput, logAutoscroll, logText);
        updateLogViewer(workflowLogOutput, workflowLogAutoscroll, logText);
        if (clearLogsBtn) clearLogsBtn.disabled = Boolean(isRunning);
        if (logRunningHint) logRunningHint.hidden = !isRunning;
      }

      function getNodeElement(id) {
        return stage.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
      }

      function drawConnections() {
        svg.replaceChildren();
        graph.edges.forEach((edge) => {
          const from = getNodeElement(edge.from);
          const to = getNodeElement(edge.to);
          if (!from || !to) return;
          const stageRect = stage.getBoundingClientRect();
          const fromRect = from.querySelector(".node-port.output").getBoundingClientRect();
          const toRect = to.querySelector(".node-port.input").getBoundingClientRect();
          const x1 = fromRect.left - stageRect.left + fromRect.width / 2;
          const y1 = fromRect.top - stageRect.top + fromRect.height / 2;
          const x2 = toRect.left - stageRect.left + toRect.width / 2;
          const y2 = toRect.top - stageRect.top + toRect.height / 2;
          const bend = Math.max(55, Math.abs(x2 - x1) * 0.45);
          const pathData = `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
          const visible = document.createElementNS("http://www.w3.org/2000/svg", "path");
          visible.setAttribute("d", pathData);
          visible.setAttribute("class", `connection${selectedEdge === edge.id ? " selected" : ""}`);
          const hitArea = document.createElementNS("http://www.w3.org/2000/svg", "path");
          hitArea.setAttribute("d", pathData);
          hitArea.setAttribute("class", "connection-hit");
          hitArea.addEventListener("click", () => {
            selectedEdge = edge.id;
            selectedNode = null;
            render();
          });
          svg.append(visible, hitArea);
        });
      }

      function render() {
        stage.querySelectorAll(".flow-node").forEach((node) => node.remove());
        graph.nodes.forEach((node) => {
          const definition = definitions[node.type];
          const card = document.createElement("article");
          card.className = "flow-node";
          card.dataset.nodeId = node.id;
          card.style.left = `${node.x}px`;
          card.style.top = `${node.y}px`;
          if (selectedNode === node.id) card.classList.add("selected");
          if (runningNode === node.id) card.classList.add("running");

          const input = document.createElement("button");
          input.type = "button";
          input.className = "node-port input";
          input.setAttribute("aria-label", `Nối đầu vào ${definition.title}`);
          input.addEventListener("click", () => connectTo(node.id));
          const output = document.createElement("button");
          output.type = "button";
          output.className = `node-port output${connectingFrom === node.id ? " connecting" : ""}`;
          output.setAttribute("aria-label", `Chọn đầu ra ${definition.title}`);
          output.addEventListener("click", () => {
            connectingFrom = connectingFrom === node.id ? null : node.id;
            selectedNode = node.id;
            selectedEdge = null;
            tell(connectingFrom ? "Đã chọn cổng ra. Chọn cổng vào của node đích để nối." : "Đã hủy nối node.");
            render();
          });

          const heading = document.createElement("div");
          heading.className = "node-heading";
          const icon = document.createElement("span");
          icon.className = "node-icon";
          icon.textContent = definition.icon;
          const title = document.createElement("h3");
          title.textContent = definition.title;
          heading.append(icon, title);
          heading.addEventListener("pointerdown", (event) => {
            if (event.button !== 0) return;
            selectedNode = node.id;
            selectedEdge = null;
            const startX = event.clientX;
            const startY = event.clientY;
            const originalX = node.x;
            const originalY = node.y;
            heading.setPointerCapture(event.pointerId);
            const move = (moveEvent) => {
              node.x = Math.max(0, originalX + moveEvent.clientX - startX);
              node.y = Math.max(0, originalY + moveEvent.clientY - startY);
              card.style.left = `${node.x}px`;
              card.style.top = `${node.y}px`;
              drawConnections();
            };
            const finish = () => {
              heading.removeEventListener("pointermove", move);
              heading.removeEventListener("pointerup", finish);
              heading.removeEventListener("pointercancel", finish);
              saveGraph();
              render();
            };
            heading.addEventListener("pointermove", move);
            heading.addEventListener("pointerup", finish);
            heading.addEventListener("pointercancel", finish);
          });

          const detail = document.createElement("p");
          detail.className = "node-detail muted";
          detail.textContent = definition.detail;
          const state = document.createElement("small");
          state.className = "node-state";
          state.textContent = getNodeProgress(node.id, node.type).label;
          card.classList.add(...getNodeProgress(node.id, node.type).classes);
          const controls = document.createElement("div");
          controls.className = "node-controls";
          const run = document.createElement("button");
          run.type = "button";
          run.className = "run-node";
          run.textContent = "Chạy node";
          run.disabled = Boolean(runningNode || processMode);
          run.title = `Chạy --${definition.mode}`;
          run.addEventListener("click", () => runNode(node));
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "remove-node";
          remove.textContent = "×";
          remove.title = "Xóa node";
          remove.setAttribute("aria-label", `Xóa ${definition.title}`);
          remove.addEventListener("click", () => removeNode(node.id));
          controls.append(run, remove);
          if (node.type === "automation") {
            const currentMode = node.schedule_mode || "datetime";
            const currentMins = Number(node.interval_minutes) || 360;
            const currentTm = node.schedule_time || "19:00";
            let currentDt = node.schedule_datetime || "";

            if (!currentDt) {
              const nextH = new Date(Date.now() + 3600000);
              nextH.setMinutes(0, 0, 0);
              const yr = nextH.getFullYear();
              const mo = String(nextH.getMonth() + 1).padStart(2, "0");
              const da = String(nextH.getDate()).padStart(2, "0");
              const ho = String(nextH.getHours()).padStart(2, "0");
              const mi = String(nextH.getMinutes()).padStart(2, "0");
              currentDt = `${yr}-${mo}-${da}T${ho}:${mi}`;
              node.schedule_datetime = currentDt;
            }

            if (currentMode === "datetime") {
              const d = new Date(currentDt);
              const formatted = isNaN(d.getTime())
                ? currentDt
                : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ngày ${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
              detail.textContent = `Chạy lúc: ${formatted}.`;
            } else if (currentMode === "daily") {
              detail.textContent = `Chạy cố định hàng ngày lúc ${currentTm}.`;
            } else {
              detail.textContent = `Lặp lại sau mỗi ${currentMins >= 60 ? (currentMins / 60) + ' giờ' : currentMins + ' phút'}.`;
            }

            const schedWrap = document.createElement("div");
            schedWrap.className = "node-schedule-wrap";
            schedWrap.style.cssText = "margin: 6px 0; padding: 6px 8px; border-radius: 6px; background: rgba(15,23,42,0.65); border: 1px solid rgba(148,163,184,0.25);";

            const modeLabel = document.createElement("label");
            modeLabel.style.cssText = "display: block; font-size: 11px; color: #93c5fd; font-weight: 600; margin-bottom: 4px;";
            modeLabel.textContent = "⚙️ Kiểu lịch chạy (Schedule):";

            const modeSelect = document.createElement("select");
            modeSelect.style.cssText = "width: 100%; font-size: 11px; font-weight: 600; padding: 4px 6px; border-radius: 4px; background: #0f172a; color: #38bdf8; border: 1px solid #38bdf8; outline: none; cursor: pointer; margin-bottom: 6px;";
            const modeOpts = [
              ["datetime", "📅 Theo ngày & giờ cụ thể"],
              ["daily", "⏰ Cố định hàng ngày"],
              ["interval", "⏱️ Lặp lại theo chu kỳ"]
            ];
            modeOpts.forEach(([val, lbl]) => {
              const opt = document.createElement("option");
              opt.value = val;
              opt.textContent = lbl;
              if (val === currentMode) opt.selected = true;
              modeSelect.append(opt);
            });
            modeSelect.addEventListener("pointerdown", (e) => e.stopPropagation());
            modeSelect.addEventListener("change", (e) => {
              e.stopPropagation();
              node.schedule_mode = modeSelect.value;
              saveGraph();
              render();
              tell(`Đã chuyển lịch chạy sang: ${modeSelect.options[modeSelect.selectedIndex].text}.`);
            });

            schedWrap.append(modeLabel, modeSelect);

            if (currentMode === "datetime") {
              const dtLabel = document.createElement("label");
              dtLabel.style.cssText = "display: block; font-size: 10px; color: #cbd5e1; margin-bottom: 2px;";
              dtLabel.textContent = "Chọn ngày tháng & giờ chạy:";
              const dtInput = document.createElement("input");
              dtInput.type = "datetime-local";
              dtInput.value = currentDt;
              dtInput.style.cssText = "width: 100%; font-size: 11px; padding: 4px 6px; border-radius: 4px; background: #1e293b; color: #f8fafc; border: 1px solid #475569; outline: none; cursor: pointer;";
              dtInput.addEventListener("pointerdown", (e) => e.stopPropagation());
              dtInput.addEventListener("change", (e) => {
                e.stopPropagation();
                node.schedule_datetime = dtInput.value;
                saveGraph();
                const d = new Date(dtInput.value);
                const formatted = isNaN(d.getTime())
                  ? dtInput.value
                  : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ngày ${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
                detail.textContent = `Chạy lúc: ${formatted}.`;
                tell(`Đã đặt lịch chạy Flow: ${formatted}.`);
              });
              schedWrap.append(dtLabel, dtInput);
            } else if (currentMode === "daily") {
              const tmLabel = document.createElement("label");
              tmLabel.style.cssText = "display: block; font-size: 10px; color: #cbd5e1; margin-bottom: 2px;";
              tmLabel.textContent = "Giờ chạy mỗi ngày:";
              const tmInput = document.createElement("input");
              tmInput.type = "time";
              tmInput.value = currentTm;
              tmInput.style.cssText = "width: 100%; font-size: 11px; padding: 4px 6px; border-radius: 4px; background: #1e293b; color: #f8fafc; border: 1px solid #475569; outline: none; cursor: pointer;";
              tmInput.addEventListener("pointerdown", (e) => e.stopPropagation());
              tmInput.addEventListener("change", (e) => {
                e.stopPropagation();
                node.schedule_time = tmInput.value || "19:00";
                saveGraph();
                detail.textContent = `Chạy cố định hàng ngày lúc ${node.schedule_time}.`;
                tell(`Đã đặt lịch chạy Flow hàng ngày vào lúc ${node.schedule_time}.`);
              });
              schedWrap.append(tmLabel, tmInput);
            } else {
              const intLabel = document.createElement("label");
              intLabel.style.cssText = "display: block; font-size: 10px; color: #cbd5e1; margin-bottom: 2px;";
              intLabel.textContent = "Chu kỳ lặp lại:";
              const select = document.createElement("select");
              select.style.cssText = "width: 100%; font-size: 11px; padding: 4px 6px; border-radius: 4px; background: #1e293b; color: #f8fafc; border: 1px solid #475569; outline: none; cursor: pointer;";
              const intervals = [
                [15, "Mỗi 15 phút"],
                [30, "Mỗi 30 phút"],
                [60, "Mỗi 1 giờ"],
                [120, "Mỗi 2 giờ"],
                [180, "Mỗi 3 giờ"],
                [360, "Mỗi 6 giờ (mặc định)"],
                [720, "Mỗi 12 giờ"],
                [1440, "Mỗi 24 giờ (1 ngày)"]
              ];
              intervals.forEach(([val, lbl]) => {
                const opt = document.createElement("option");
                opt.value = String(val);
                opt.textContent = lbl;
                if (val === currentMins) opt.selected = true;
                select.append(opt);
              });
              select.addEventListener("pointerdown", (e) => e.stopPropagation());
              select.addEventListener("change", (e) => {
                e.stopPropagation();
                node.interval_minutes = Number(select.value) || 360;
                saveGraph();
                const selText = select.options[select.selectedIndex].text;
                detail.textContent = `Lặp lại sau ${selText}.`;
                tell(`Đã đặt lịch chạy Flow: ${selText}.`);
              });
              schedWrap.append(intLabel, select);
            }

            card.append(input, output, heading, detail, schedWrap, state, controls);
          } else {
            card.append(input, output, heading, detail, state, controls);
          }
          stage.append(card);
        });
        drawConnections();
      }

      function connectTo(targetId) {
        if (!connectingFrom) {
          tell("Chọn cổng ra bên phải node nguồn trước.", true);
          return;
        }
        if (connectingFrom === targetId) {
          tell("Không thể nối node với chính nó.", true);
          return;
        }
        if (graph.edges.some((edge) => edge.from === connectingFrom && edge.to === targetId)) {
          tell("Hai node này đã được nối.");
          connectingFrom = null;
          render();
          return;
        }
        graph.edges.push({
          id: `${connectingFrom}-${targetId}-${Date.now()}`,
          from: connectingFrom,
          to: targetId
        });
        connectingFrom = null;
        selectedEdge = null;
        saveGraph();
        tell("Đã nối hai node.");
        render();
      }

      function addNode(type, x, y) {
        if (!definitions[type]) return;
        const node = {
          id: `node-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
          type,
          x: Math.max(20, Math.min(x, 1080)),
          y: Math.max(24, Math.min(y, 550))
        };
        if (type === "automation") {
          node.schedule_mode = "datetime";
          const nextH = new Date(Date.now() + 3600000);
          nextH.setMinutes(0, 0, 0);
          const yr = nextH.getFullYear();
          const mo = String(nextH.getMonth() + 1).padStart(2, "0");
          const da = String(nextH.getDate()).padStart(2, "0");
          const ho = String(nextH.getHours()).padStart(2, "0");
          const mi = String(nextH.getMinutes()).padStart(2, "0");
          node.schedule_datetime = `${yr}-${mo}-${da}T${ho}:${mi}`;
          node.schedule_time = "19:00";
          node.interval_minutes = 360;
        }
        graph.nodes.push(node);
        selectedNode = node.id;
        selectedEdge = null;
        saveGraph();
        render();
      }

      function removeNode(id) {
        graph.nodes = graph.nodes.filter((node) => node.id !== id);
        graph.edges = graph.edges.filter((edge) => edge.from !== id && edge.to !== id);
        selectedNode = null;
        selectedEdge = null;
        saveGraph();
        render();
        tell("Đã xóa node và các kết nối liên quan.");
      }

      async function runNode(node) {
        runningNode = node.id;
        processMode = definitions[node.type].mode;
        tell(`Đang khởi chạy ${definitions[node.type].title}…`);
        render();
        const endpoint = stage.dataset.runUrl.replace("__node_type__", encodeURIComponent(node.type));
        const bodyParams = { csrf_token: stage.dataset.csrfToken };
        if (node.type === "automation") {
          bodyParams.schedule_mode = node.schedule_mode || "datetime";
          if (node.schedule_datetime) bodyParams.schedule_datetime = String(node.schedule_datetime);
          if (node.schedule_time) bodyParams.schedule_time = String(node.schedule_time);
          if (node.interval_minutes) bodyParams.interval_minutes = String(node.interval_minutes);
        }
        try {
          const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams(bodyParams)
          });
          const result = await safeParseJson(response, "Không chạy được node.");
          tell(`${result.message} Tiến trình được quản lý ở mục trạng thái workflow.`);
          updateProcessStatus(result.process, result.workflow);
        } catch (error) {
          runningNode = null;
          processMode = null;
          tell(error.message || "Có lỗi khi khởi chạy node.", true);
          render();
        }
      }

      async function runCustomIdeas(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const formStatus = document.getElementById("custom-idea-status");
        const submit = document.getElementById("generate-custom-ideas");
        if (processMode || runningNode) {
          formStatus.textContent = "Hãy dừng hoặc chờ workflow hiện tại trước.";
          return;
        }
        let node = graph.nodes.find((item) => item.type === "generate-ideas");
        if (!node) {
          addNode("generate-ideas", 100, 90);
          node = graph.nodes.find((item) => item.type === "generate-ideas");
        }
        if (!node) {
          formStatus.textContent = "Không thể tạo node sinh ý tưởng.";
          return;
        }

        runningNode = node.id;
        processMode = "generate-ideas";
        submit.disabled = true;
        formStatus.textContent = "Đang khởi chạy…";
        tell("Đang gửi prompt và thông số tạo ý tưởng.");
        render();
        try {
          const response = await fetch(form.action, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams(new FormData(form))
          });
          const result = await safeParseJson(response, "Không chạy được tạo ý tưởng.");
          formStatus.textContent = result.message;
          tell(`${result.message} Tiến trình sẽ tiếp tục lắng nghe Telegram; dừng khi không cần nữa.`);
          updateProcessStatus(result.process);
        } catch (error) {
          runningNode = null;
          processMode = null;
          formStatus.textContent = error.message || "Có lỗi khi khởi chạy.";
          tell(formStatus.textContent, true);
          render();
        } finally {
          submit.disabled = Boolean(processMode || runningNode);
        }
      }

      function getNodeProgress(nodeId, nodeType) {
        if (workflowProgress) {
          if (workflowProgress.current_node === nodeId && workflowProgress.running) {
            return {
              label:
                nodeType === "approval-video" || nodeType === "approval-publish"
                  ? "Đang chờ duyệt / xử lý…"
                  : "Đang chạy…",
              classes: ["running"]
            };
          }
          if (workflowProgress.completed_nodes?.includes(nodeId)) {
            return { label: "Hoàn tất", classes: ["success"] };
          }
          if (workflowProgress.error && workflowProgress.current_node === nodeId) {
            return { label: "Lỗi khi chạy", classes: ["error"] };
          }
        }
        if (runningNode === nodeId) return { label: "Đang khởi chạy…", classes: ["running"] };
        return { label: "Sẵn sàng", classes: [] };
      }

      function updateWorkflowNodes() {
        graph.nodes.forEach((node) => {
          const element = getNodeElement(node.id);
          if (!element) return;
          element.classList.remove("running", "success", "error");
          const progress = getNodeProgress(node.id, node.type);
          element.classList.add(...progress.classes);
          element.querySelector(".node-state").textContent = progress.label;
        });
      }

      function updateProcessStatus(process, workflow = workflowProgress) {
        const previousMode = processMode;
        workflowProgress =
          process.mode === "run-flow" ||
          workflow?.running ||
          (previousMode === "run-flow" && !process.running) ||
          (!process.running && !process.mode && workflow?.node_order)
            ? workflow || null
            : null;
        if (process.running) {
          processMode = process.mode;
          if (runningNode && definitions[graph.nodes.find((node) => node.id === runningNode)?.type]?.mode !== process.mode) {
            runningNode = null;
          }
          if (process.mode === "run-flow") {
            const current = graph.nodes.find((node) => node.id === workflowProgress?.current_node);
            statusPill.textContent = current
              ? `Đang chạy flow · ${definitions[current.type].title}`
              : "Đang chạy flow…";
          } else {
            statusPill.textContent = `Đang chạy · --${process.mode}`;
          }
          statusPill.className = "pill warn";
          if (stopProcessButton) stopProcessButton.textContent = "Dừng tiến trình";
        } else {
          statusPill.textContent = "Đã dừng";
          statusPill.className = "pill muted";
          if (stopProcessButton) stopProcessButton.textContent = "Dừng / Dọn tiến trình ngầm";
          if (runningNode) {
            const node = getNodeElement(runningNode);
            if (node) {
              node.classList.remove("running");
              node.classList.add(process.last_exit_code === 0 ? "success" : "error");
              const state = node.querySelector(".node-state");
              state.textContent = process.last_exit_code === 0 ? "Hoàn tất" : `Kết thúc lỗi (${process.last_exit_code ?? "không rõ"})`;
            }
            if (process.last_exit_code === 0) tell("Node đã chạy xong.");
            else tell("Node kết thúc với lỗi. Xem log để biết chi tiết.", true);
            runningNode = null;
          }
          processMode = null;
          if (previousMode === "run-flow" && workflowProgress?.error) {
            tell(`Flow dừng do lỗi: ${workflowProgress.error}`, true);
          } else if (previousMode === "run-flow" && workflowProgress?.completed_nodes?.length) {
            tell("Flow đã hoàn tất.");
          }
        }
        updateWorkflowNodes();
        renderRunButtons();
      }

      function renderRunButtons() {
        const busy = Boolean(runningNode || processMode);
        stage.querySelectorAll(".run-node").forEach((button) => {
          button.disabled = busy;
        });
        document.getElementById("generate-custom-ideas").disabled = busy;
        document.getElementById("run-workflow").disabled = busy;
        if (openFlowLoginButton) openFlowLoginButton.disabled = processMode === "flow-login";
        if (produceQueueTelegramBtn) produceQueueTelegramBtn.disabled = busy || currentQueueCount === 0;
        if (clearQueueBtn) clearQueueBtn.disabled = currentQueueCount === 0;
        if (resendDraftsTelegramBtn) resendDraftsTelegramBtn.disabled = busy || currentDraftCount === 0;
        document.querySelectorAll(".js-draft-produce-telegram, .js-draft-produce-publish").forEach((btn) => {
          const canQueueIntoRunningListener =
            processMode === "run-flow" ||
            processMode === "approval-video" ||
            processMode === "approval-publish" ||
            processMode === "auto" ||
            processMode === "generate-ideas" ||
            processMode === "resend-drafts";
          btn.disabled = busy && !canQueueIntoRunningListener;
        });
      }

      async function runWorkflow() {
        if (!graph.nodes.length) {
          tell("Thêm ít nhất một node trước khi chạy flow.", true);
          return;
        }
        const button = document.getElementById("run-workflow");
        const form = document.getElementById("custom-idea-form");
        const body = new URLSearchParams(new FormData(form));
        const autoNode = graph.nodes.find((n) => n.type === "automation");
        if (autoNode) {
          body.set("schedule_mode", autoNode.schedule_mode || "datetime");
          if (autoNode.schedule_datetime) body.set("schedule_datetime", String(autoNode.schedule_datetime));
          if (autoNode.schedule_time) body.set("schedule_time", String(autoNode.schedule_time));
          if (autoNode.interval_minutes) body.set("interval_minutes", String(autoNode.interval_minutes));
        }
        body.set("nodes", JSON.stringify(graph.nodes.map(({ id, type, schedule_mode, schedule_datetime, schedule_time, interval_minutes }) => ({ id, type, schedule_mode, schedule_datetime, schedule_time, interval_minutes }))));
        body.set("edges", JSON.stringify(graph.edges.map(({ from, to }) => ({ from, to }))));
        button.disabled = true;
        processMode = "starting-flow";
        renderRunButtons();
        tell("Đang khởi chạy flow theo thứ tự các kết nối…");
        try {
          const response = await fetch(stage.dataset.flowUrl, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body
          });
          const result = await safeParseJson(response, "Không chạy được flow.");
          processMode = "run-flow";
          workflowProgress = result.workflow;
          tell(`${result.message} Các node sẽ chạy lần lượt theo kết nối.`);
          updateProcessStatus(result.process, result.workflow);
        } catch (error) {
          processMode = null;
          tell(error.message || "Có lỗi khi khởi chạy flow.", true);
          renderRunButtons();
        }
      }

      async function postAction(url) {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ csrf_token: stage.dataset.csrfToken })
        });
        return await safeParseJson(response, "Thao tác thất bại.");
      }

      const authErrorBanner = document.getElementById("flow-auth-error-banner");
      document.querySelectorAll(".js-run-save-flow-auth").forEach((btn) => {
        btn.addEventListener("click", async () => {
          try {
            const loginUrl = btn.dataset.loginUrl || openFlowLoginButton?.dataset.loginUrl;
            const result = await postAction(loginUrl);
            if (authErrorBanner) authErrorBanner.hidden = true;
            tell(result.message);
            tellSavedIdeas(result.message, false);
            updateProcessStatus(result.process);
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
        });
      });

      if (produceQueueTelegramBtn) {
        produceQueueTelegramBtn.addEventListener("click", async () => {
          try {
            const result = await postAction(stage.dataset.produceQueueUrl);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            updateProcessStatus(result.process);
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
        });
      }

      if (clearQueueBtn) {
        clearQueueBtn.addEventListener("click", async () => {
          try {
            const result = await postAction(stage.dataset.clearQueueUrl);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            document.querySelectorAll(".queued-item-row").forEach((el) => el.remove());
            const box = document.getElementById("workflow-approved-queue-box");
            if (box) box.remove();
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
        });
      }

      if (resendDraftsTelegramBtn) {
        resendDraftsTelegramBtn.addEventListener("click", async () => {
          try {
            const result = await postAction(stage.dataset.resendDraftsUrl);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            updateProcessStatus(result.process);
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
        });
      }

      document.addEventListener("click", async (event) => {
        const btnPreset = event.target.closest(".js-apply-flow-preset");
        if (btnPreset) {
          const preset = btnPreset.dataset.preset;
          if (preset === "approve-publish") {
            graph = {
              nodes: [
                { id: "ideas", type: "generate-ideas", x: 55, y: 145 },
                { id: "approval-video", type: "approval-video", x: 350, y: 145 },
                { id: "process-queue", type: "process-queue", x: 655, y: 145 }
              ],
              edges: [
                { id: "ideas-approval-video", from: "ideas", to: "approval-video" },
                { id: "approval-video-process-queue", from: "approval-video", to: "process-queue" }
              ]
            };
            tell("⚡ Đã nạp Flow 3 bước: Tạo ý tưởng ➔ Duyệt (Web/Telegram) ➔ Sản xuất & Tự Đăng TikTok, FB Reels, YouTube Shorts. Bấm '▶ Chạy flow' để bắt đầu!");
          } else if (preset === "full-auto-publish") {
            graph = {
              nodes: [
                { id: "ideas", type: "generate-ideas", x: 90, y: 145 },
                { id: "process-queue", type: "process-queue", x: 435, y: 145 }
              ],
              edges: [
                { id: "ideas-process-queue", from: "ideas", to: "process-queue" }
              ]
            };
            tell("🤖 Đã nạp Flow Full Auto 100%: Tạo ý tưởng ➔ Tự động Approve ➔ Tạo Video 3 Scene ➔ Tự Đăng TikTok, FB Reels, YouTube Shorts. Bấm '▶ Chạy flow' để bắt đầu!");
          } else {
            graph = structuredClone(defaultGraph);
            tell("📩 Đã nạp Flow: Tạo ý tưởng ➔ Duyệt (Web/Telegram) ➔ Tạo Video ➔ Gửi Telegram. Bấm '▶ Chạy flow' để bắt đầu!");
          }
          selectedNode = null;
          selectedEdge = null;
          connectingFrom = null;
          saveGraph();
          render();
          return;
        }

        const btnTelegram = event.target.closest(".js-draft-produce-telegram");
        if (btnTelegram) {
          const draftId = btnTelegram.dataset.draftId;
          const url = stage.dataset.draftTelegramUrl.replace("__draft_id__", encodeURIComponent(draftId));
          try {
            const result = await postAction(url);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            if (result.process) updateProcessStatus(result.process);
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
          return;
        }

        const btnPublish = event.target.closest(".js-draft-produce-publish");
        if (btnPublish) {
          const draftId = btnPublish.dataset.draftId;
          const url = stage.dataset.draftPublishUrl.replace("__draft_id__", encodeURIComponent(draftId));
          try {
            const result = await postAction(url);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            if (result.process) updateProcessStatus(result.process);
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
          return;
        }

        const btnApprove = event.target.closest(".js-draft-approve-only");
        if (btnApprove) {
          const draftId = btnApprove.dataset.draftId;
          const url = stage.dataset.draftApproveUrl.replace("__draft_id__", encodeURIComponent(draftId));
          try {
            const result = await postAction(url);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
          return;
        }

        const btnQueueDelete = event.target.closest(".js-queue-delete");
        if (btnQueueDelete) {
          const queueId = btnQueueDelete.dataset.queueId;
          const url = stage.dataset.queueDeleteUrl.replace("__item_id__", encodeURIComponent(queueId));
          try {
            const result = await postAction(url);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            document.querySelectorAll(`.queued-item-row[data-queue-id="${CSS.escape(queueId)}"]`).forEach((el) => el.remove());
            if (!document.querySelectorAll("#workflow-approved-queue-box .queued-item-row").length) {
              const box = document.getElementById("workflow-approved-queue-box");
              if (box) box.remove();
            }
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
          return;
        }

        const btnDraftDelete = event.target.closest(".js-draft-delete");
        if (btnDraftDelete) {
          const draftId = btnDraftDelete.dataset.draftId;
          const url = stage.dataset.draftDeleteUrl.replace("__draft_id__", encodeURIComponent(draftId));
          try {
            const result = await postAction(url);
            tell(result.message);
            tellSavedIdeas(result.message, false);
            document.querySelectorAll(`.saved-draft-item[data-draft-id="${CSS.escape(draftId)}"]`).forEach((el) => el.remove());
            document.querySelectorAll(`.queued-item-row[data-queue-id="${CSS.escape(draftId)}"]`).forEach((el) => el.remove());
            await refreshProcessStatus();
          } catch (error) {
            tell(error.message, true);
            tellSavedIdeas(error.message, true);
          }
        }
      });

      async function refreshProcessStatus() {
        try {
          const response = await fetch(stage.dataset.statusUrl, { cache: "no-store" });
          const result = await safeParseJson(response, "Không lấy được trạng thái workflow.");
          if (typeof result.queue_count === "number") {
            currentQueueCount = result.queue_count;
            if (queueCountBadge) queueCountBadge.textContent = String(result.queue_count);
          }
          if (typeof result.draft_count === "number") {
            currentDraftCount = result.draft_count;
          }
          if (workflowDraftsPill && typeof result.draft_count === "number" && typeof result.queue_count === "number") {
            workflowDraftsPill.textContent = `${result.draft_count} bản nháp · ${result.queue_count} đã duyệt`;
          }
          if (Array.isArray(result.drafts)) {
            const stateLabels = {
              approved: "✅ Đã Approve",
              processed: "✓ Đã tạo video",
              skipped: "❌ Đã bỏ qua",
              pending: "⏳ Chờ duyệt"
            };
            const savedListContainer = document.getElementById("workflow-saved-drafts-list");
            const existingIds = savedListContainer
              ? Array.from(savedListContainer.querySelectorAll(".saved-draft-item")).map((el) => el.dataset.draftId)
              : [];
            const incomingIds = result.drafts.map((d) => String(d.id || ""));
            const idsChanged =
              savedListContainer &&
              (existingIds.length !== incomingIds.length ||
                existingIds.some((id, idx) => id !== incomingIds[idx]));

            if (idsChanged && savedListContainer) {
              if (!result.drafts.length) {
                savedListContainer.innerHTML = '<p class="muted">Chưa có ý tưởng nào được lưu.</p>';
              } else {
                const itemsHtml = result.drafts
                  .map((draft) => {
                    const draftId = escapeHtml(draft.id || "");
                    const state = draft.state || "pending";
                    const stateLabel = escapeHtml(stateLabels[state] || state);
                    const pillClass = state === "approved" || state === "processed" ? "ok" : "muted";
                    const idea = draft.idea || {};
                    const title = escapeHtml(idea.title || "Không có tiêu đề");
                    const scenes = Array.isArray(idea.scenes) ? idea.scenes : [];
                    const durationSec = escapeHtml(idea.duration_seconds || 20);
                    const fullScript = idea.full_script_text
                      ? `<pre style="white-space:pre-wrap; margin:0 0 8px 0; font-family:inherit; color:#e2e8f0;">${escapeHtml(idea.full_script_text)}</pre>`
                      : scenes
                          .map(
                            (sc, idx) =>
                              `<div style="margin-bottom:10px;"><strong>Scene ${idx + 1}${sc.scene_title ? ": " + escapeHtml(sc.scene_title) : ""} (${escapeHtml(sc.duration || "")})</strong><div>${escapeHtml(sc.prompt || "")}</div></div>`
                          )
                          .join("");
                    return `
                      <li class="saved-draft-item" data-draft-id="${draftId}">
                        <div class="saved-draft-info" style="flex: 1 1 340px;">
                          <strong>${title}</strong>
                          <span class="pill tiny-pill ${pillClass}">${stateLabel}</span>
                          <br>
                          <small class="muted">ID: <code>${draftId}</code> · ${scenes.length} cảnh · ${durationSec}s</small>
                          <details class="script-preview" style="margin-top:6px;">
                            <summary style="cursor:pointer; font-size:12px; color:#60a5fa; user-select:none;">👁 Xem chi tiết kịch bản 5 phần (${scenes.length} cảnh)</summary>
                            <div style="margin-top:6px; padding:8px 10px; background:rgba(15,23,42,0.55); border:1px solid rgba(148,163,184,0.2); border-radius:8px; font-size:12px; line-height:1.5;">
                              ${fullScript}
                            </div>
                          </details>
                        </div>
                        <div class="saved-draft-actions">
                          <button type="button" class="tiny js-draft-produce-telegram" data-draft-id="${draftId}">
                            ✓ Tạo video → Telegram
                          </button>
                          <button type="button" class="secondary tiny js-draft-produce-publish" data-draft-id="${draftId}">
                            ▶ Tạo &amp; Đăng
                          </button>
                          <button type="button" class="secondary tiny js-draft-approve-only" data-draft-id="${draftId}">
                            ${state === "approved" ? "✅ Đã Approve" : "+ Duyệt vào hàng đợi"}
                          </button>
                          <button type="button" class="danger tiny js-draft-delete" data-draft-id="${draftId}">
                            🗑 Xóa
                          </button>
                        </div>
                      </li>`;
                  })
                  .join("");
                savedListContainer.innerHTML = `<ol class="queue-list saved-drafts-list">${itemsHtml}</ol>`;
              }
            } else {
              result.drafts.forEach((d) => {
                const state = d.state || "pending";
                const items = document.querySelectorAll(`.saved-draft-item[data-draft-id="${CSS.escape(d.id || "")}"]`);
                items.forEach((item) => {
                  const pill = item.querySelector(".tiny-pill");
                  if (pill) {
                    pill.textContent = stateLabels[state] || state;
                    pill.className = `pill tiny-pill ${state === "approved" || state === "processed" ? "ok" : "muted"}`;
                  }
                  const approveBtn = item.querySelector(".js-draft-approve-only");
                  if (approveBtn) {
                    approveBtn.textContent = state === "approved" ? "✅ Đã Approve" : "+ Duyệt vào hàng đợi";
                  }
                });
              });
            }
          }
          const hasAuthError = Boolean(
            result.workflow?.auth_error ||
            (result.logs && result.logs.includes("save_flow_auth.py") && result.process?.mode !== "flow-login")
          );
          if (authErrorBanner) {
            authErrorBanner.hidden = !hasAuthError;
            authErrorBanner.style.display = hasAuthError ? "flex" : "none";
          }
          updateLogs(result.logs || "", Boolean(result.process?.running));
          updateProcessStatus(result.process, result.workflow);
        } catch (error) {
          tell(error.message || "Không lấy được trạng thái workflow.", true);
        }
      }

      if (refreshLogsBtn) refreshLogsBtn.addEventListener("click", refreshProcessStatus);
      if (workflowRefreshLogsBtn) workflowRefreshLogsBtn.addEventListener("click", refreshProcessStatus);

      document.querySelectorAll(".palette-node").forEach((button) => {
        button.addEventListener("click", () => {
          const offset = graph.nodes.length;
          addNode(button.dataset.nodeType, 100 + (offset % 3) * 270, 90 + Math.floor(offset / 3) * 170);
        });
        button.addEventListener("dragstart", (event) => {
          event.dataTransfer.setData("text/plain", button.dataset.nodeType);
          event.dataTransfer.effectAllowed = "copy";
        });
      });

      stage.addEventListener("dragover", (event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      });
      stage.addEventListener("drop", (event) => {
        event.preventDefault();
        const rect = stage.getBoundingClientRect();
        addNode(event.dataTransfer.getData("text/plain"), event.clientX - rect.left, event.clientY - rect.top);
      });

      document.getElementById("reset-workflow").addEventListener("click", () => {
        graph = structuredClone(defaultGraph);
        selectedNode = null;
        selectedEdge = null;
        connectingFrom = null;
        saveGraph();
        render();
        tell("Đã khôi phục flow mẫu.");
      });
      document.getElementById("run-workflow").addEventListener("click", runWorkflow);
      document.getElementById("custom-idea-form").addEventListener("submit", runCustomIdeas);

      document.addEventListener("keydown", (event) => {
        if (event.key !== "Delete" && event.key !== "Backspace") return;
        if (
          event.target instanceof HTMLInputElement ||
          event.target instanceof HTMLTextAreaElement ||
          event.target instanceof HTMLSelectElement
        ) {
          return;
        }
        if (selectedNode) removeNode(selectedNode);
        else if (selectedEdge) {
          graph.edges = graph.edges.filter((edge) => edge.id !== selectedEdge);
          selectedEdge = null;
          saveGraph();
          render();
          tell("Đã xóa kết nối.");
        }
      });

      // --- Multi-platform Video Import & Publishing Studio ---
      const publishForm = document.getElementById("multiplatform-publish-form");
      const publishFileInput = document.getElementById("publish-video-file");
      const publishLocalPathInput = document.getElementById("publish-local-path");
      const publishVideoSelect = document.getElementById("publish-video-select");
      const publishGeneralTitle = document.getElementById("publish-general-title");
      const publishAiHintInput = document.getElementById("publish-ai-hint");
      const generateGeminiCaptionsBtn = document.getElementById("generate-gemini-captions-btn");
      const publishPreviewVideo = document.getElementById("publish-video-preview");
      const publishPreviewLabel = document.getElementById("publish-preview-label");
      const publishStatusBox = document.getElementById("publish-multiplatform-status");
      const publishSubmitBtn = document.getElementById("publish-multiplatform-submit");
      const importVideoOnlyBtn = document.getElementById("import-video-only-btn");
      const draftAutofillSelect = document.getElementById("publish-draft-autofill");
      const applyDraftAutofillBtn = document.getElementById("apply-draft-autofill-btn");
      const copyTiktokToAllBtn = document.getElementById("copy-tiktok-to-all-btn");
      let previewObjectUrl = null;

      function showPublishStatus(htmlOrText, isError = false, asHtml = false) {
        if (!publishStatusBox) return;
        publishStatusBox.hidden = false;
        publishStatusBox.className = `publish-status-box ${isError ? "err-box" : "ok-box"}`;
        if (asHtml) {
          publishStatusBox.innerHTML = htmlOrText;
        } else {
          publishStatusBox.textContent = htmlOrText;
        }
      }

      function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = String(text ?? "");
        return div.innerHTML;
      }

      function buildYoutubeShortsTitle(rawTitle, rawHashtags, maxLen = 100) {
        const titleWords = String(rawTitle || "").trim().split(/\s+/).filter(Boolean);
        const baseWords = [];
        const inlineTags = [];
        titleWords.forEach((w) => {
          if (w.startsWith("#") && w.length > 1) {
            inlineTags.push(w.replace(/^#+/, ""));
          } else {
            baseWords.push(w);
          }
        });
        let baseTitle = baseWords.join(" ").trim() || String(rawTitle || "Video ngắn").trim();
        const extraTokens = String(rawHashtags || "")
          .replace(/,/g, " ")
          .split(/\s+/)
          .map((t) => t.replace(/^#+/, "").trim())
          .filter(Boolean);
        const allTags = [];
        const seen = new Set();
        [...inlineTags, ...extraTokens].forEach((tag) => {
          const lower = tag.toLowerCase();
          if (!seen.has(lower)) {
            seen.add(lower);
            allTags.push(tag);
          }
        });
        if (!seen.has("shorts")) {
          allTags.unshift("Shorts");
        } else {
          const idx = allTags.findIndex((t) => t.toLowerCase() === "shorts");
          if (idx > 0) {
            allTags.splice(idx, 1);
            allTags.unshift("Shorts");
          } else if (idx === 0) {
            allTags[0] = "Shorts";
          }
        }
        const priorityLowers = new Set(["shorts", "dunglakhi", "trending"]);
        const mustKeep = allTags.filter((t) => priorityLowers.has(t.toLowerCase())).map((t) => `#${t}`);
        const minTagsStr = (mustKeep.length ? mustKeep.slice(0, 3) : [`#${allTags[0]}`]).join(" ");
        const maxBaseLen = Math.max(20, maxLen - minTagsStr.length - 1);
        if (baseTitle.length > maxBaseLen) {
          const sliced = baseTitle.slice(0, maxBaseLen);
          const lastSpace = sliced.lastIndexOf(" ");
          baseTitle = lastSpace >= 15 ? sliced.slice(0, lastSpace).trim() : sliced.trim();
        }
        const candidates = allTags.map((t) => `#${t}`);
        const fullJoined = `${baseTitle} ${candidates.join(" ")}`.trim();
        if (fullJoined.length <= maxLen) return fullJoined;

        const reservedTail = candidates.filter((t) => t.slice(1).toLowerCase() === "trending");
        const reservedLen = reservedTail.reduce((acc, t) => acc + t.length + 1, 0);
        const selected = [];
        let curLen = baseTitle.length;
        candidates.forEach((tag) => {
          if (reservedTail.includes(tag)) return;
          if (curLen + 1 + tag.length + reservedLen <= maxLen) {
            selected.push(tag);
            curLen += 1 + tag.length;
          }
        });
        reservedTail.forEach((tag) => {
          if (!selected.includes(tag) && curLen + 1 + tag.length <= maxLen) {
            selected.push(tag);
            curLen += 1 + tag.length;
          }
        });
        if (!selected.length && candidates.length) selected.push(candidates[0]);
        return `${baseTitle} ${selected.join(" ")}`.trim().slice(0, maxLen).trim();
      }

      function applySocialPostsToForm(socialPosts, generalTitle = "") {
        if (generalTitle && publishGeneralTitle) {
          publishGeneralTitle.value = generalTitle;
        }
        if (!socialPosts || typeof socialPosts !== "object") return false;
        let filled = false;
        ["tiktok", "facebook", "youtube"].forEach((platform) => {
          const pData = socialPosts[platform];
          if (!pData || typeof pData !== "object") return;
          const tEl = document.getElementById(`${platform}-title`);
          const cEl = document.getElementById(`${platform}-caption`);
          const hEl = document.getElementById(`${platform}-hashtags`);
          if (cEl && pData.caption !== undefined) cEl.value = pData.caption;
          const hashtagsStr =
            pData.hashtags !== undefined
              ? Array.isArray(pData.hashtags)
                ? pData.hashtags.join(" ")
                : String(pData.hashtags)
              : hEl?.value || "";
          if (hEl && pData.hashtags !== undefined) {
            hEl.value = hashtagsStr;
          }
          if (tEl && pData.title !== undefined) {
            tEl.value =
              platform === "youtube"
                ? buildYoutubeShortsTitle(pData.title || generalTitle, hashtagsStr, 100)
                : pData.title;
          }
          if (pData.caption || pData.hashtags) filled = true;
        });
        return filled;
      }

      function parseSocialPostsDataset(rawJson) {
        if (!rawJson) return null;
        try {
          const parsed = JSON.parse(rawJson);
          if (parsed && typeof parsed === "object" && (parsed.tiktok || parsed.facebook || parsed.youtube)) {
            return parsed;
          }
        } catch (err) {
          return null;
        }
        return null;
      }

      function syncPlatformCardVisuals() {
        ["tiktok", "facebook", "youtube"].forEach((platform) => {
          const checkbox = document.getElementById(`${platform}-enabled`);
          const card = document.querySelector(`[data-platform-card="${platform}"]`);
          if (checkbox && card) {
            card.classList.toggle("disabled-platform", !checkbox.checked);
          }
        });
      }

      ["tiktok", "facebook", "youtube"].forEach((platform) => {
        const checkbox = document.getElementById(`${platform}-enabled`);
        if (checkbox) {
          checkbox.addEventListener("change", syncPlatformCardVisuals);
        }
      });
      syncPlatformCardVisuals();

      async function triggerImportAndAutoGemini() {
        if (!publishForm) return;
        const hasFile = publishFileInput && publishFileInput.files && publishFileInput.files.length > 0;
        const hasLocalPath = publishLocalPathInput && publishLocalPathInput.value.trim().length > 0;
        if (!hasFile && !hasLocalPath) {
          showPublishStatus("Hãy chọn file video từ máy tính hoặc nhập đường dẫn file trước.", true);
          return;
        }
        const formData = new FormData();
        formData.set("csrf_token", stage?.dataset.csrfToken || publishForm.querySelector('input[name="csrf_token"]')?.value || "");
        formData.set("auto_generate_captions", "1");
        if (publishAiHintInput && publishAiHintInput.value.trim()) {
          formData.set("hint_text", publishAiHintInput.value.trim());
        }
        if (hasFile) {
          formData.set("video_file", publishFileInput.files[0]);
        }
        if (hasLocalPath) {
          formData.set("local_path", publishLocalPathInput.value.trim());
        }
        if (importVideoOnlyBtn) importVideoOnlyBtn.disabled = true;
        if (generateGeminiCaptionsBtn) generateGeminiCaptionsBtn.disabled = true;
        showPublishStatus("⏳ Đang tải video lên và gọi Gemini AI tự động viết Caption & Hashtag riêng cho TikTok, Facebook Reels và YouTube Shorts…", false);
        try {
          const response = await fetch(publishForm.dataset.importUrl, {
            method: "POST",
            body: formData
          });
          const result = await safeParseJson(response, "Import video thất bại.");
          if (publishFileInput) publishFileInput.value = "";
          if (publishLocalPathInput) publishLocalPathInput.value = "";
          updateVideoInventoryUI(result.videos, result.video?.name);
          if (result.video) {
            if (publishPreviewVideo) publishPreviewVideo.src = result.video.url;
            if (publishPreviewLabel) {
              publishPreviewLabel.innerHTML = `Đã import & đang chọn: <code>${escapeHtml(result.video.name)}</code> (${result.video.size_mb} MB)`;
            }
          }
          if (result.social_posts) {
            applySocialPostsToForm(result.social_posts, result.video?.title || "");
          }
          showPublishStatus(`✅ ${result.message}`, false);
        } catch (error) {
          showPublishStatus(error.message || "Có lỗi khi import video.", true);
        } finally {
          if (importVideoOnlyBtn) importVideoOnlyBtn.disabled = false;
          if (generateGeminiCaptionsBtn) generateGeminiCaptionsBtn.disabled = false;
        }
      }

      async function generateCaptionsWithGemini() {
        if (!publishForm || !publishForm.dataset.generateCaptionsUrl) return;
        const formData = new FormData();
        formData.set("csrf_token", stage?.dataset.csrfToken || publishForm.querySelector('input[name="csrf_token"]')?.value || "");
        if (publishVideoSelect && publishVideoSelect.value) {
          formData.set("video_name", publishVideoSelect.value);
        }
        if (publishGeneralTitle && publishGeneralTitle.value.trim()) {
          formData.set("general_title", publishGeneralTitle.value.trim());
        }
        if (publishAiHintInput && publishAiHintInput.value.trim()) {
          formData.set("hint_text", publishAiHintInput.value.trim());
        }
        if ( publishFileInput && publishFileInput.files && publishFileInput.files.length > 0 ) {
          formData.set("video_file", publishFileInput.files[0]);
        }
        if (generateGeminiCaptionsBtn) generateGeminiCaptionsBtn.disabled = true;
        showPublishStatus("✨ Đang gọi Gemini AI phân tích và viết Caption & Hashtag riêng cho TikTok, Facebook Reels và YouTube Shorts…", false);
        try {
          const response = await fetch(publishForm.dataset.generateCaptionsUrl, {
            method: "POST",
            body: formData
          });
          const result = await safeParseJson(response, "Không tạo được caption bằng Gemini AI.");
          if (result.videos && result.video_name) {
            updateVideoInventoryUI(result.videos, result.video_name);
          }
          applySocialPostsToForm(result.social_posts, result.general_title || "");
          showPublishStatus(result.message || "✨ Đã tạo Caption & Hashtag bằng Gemini AI!", false);
        } catch (error) {
          showPublishStatus(error.message || "Lỗi khi gọi Gemini AI.", true);
        } finally {
          if (generateGeminiCaptionsBtn) generateGeminiCaptionsBtn.disabled = false;
        }
      }

      if (publishFileInput) {
        publishFileInput.addEventListener("change", () => {
          const file = publishFileInput.files && publishFileInput.files[0];
          if (!file) return;
          if (previewObjectUrl) {
            URL.revokeObjectURL(previewObjectUrl);
          }
          previewObjectUrl = URL.createObjectURL(file);
          if (publishPreviewVideo) {
            publishPreviewVideo.src = previewObjectUrl;
          }
          if (publishPreviewLabel) {
            publishPreviewLabel.innerHTML = `File từ máy tính: <code>${escapeHtml(file.name)}</code> (${(file.size / (1024 * 1024)).toFixed(2)} MB)`;
          }
          const stem = file.name.replace(/\.[^/.]+$/, "");
          if (publishGeneralTitle) {
            publishGeneralTitle.value = stem;
          }
          // Tự động import và gọi Gemini AI viết Caption & Hashtag riêng cho từng nền tảng
          triggerImportAndAutoGemini();
        });
      }

      if (publishVideoSelect) {
        publishVideoSelect.addEventListener("change", () => {
          const selectedOpt = publishVideoSelect.options[publishVideoSelect.selectedIndex];
          if (!selectedOpt || !selectedOpt.value) return;
          if (publishFileInput) publishFileInput.value = "";
          if (publishLocalPathInput) publishLocalPathInput.value = "";
          const videoUrl = selectedOpt.dataset.videoUrl || `/videos/${encodeURIComponent(selectedOpt.value)}`;
          const videoTitle = selectedOpt.dataset.videoTitle || selectedOpt.value.replace(/\.[^/.]+$/, "");
          if (publishPreviewVideo) {
            publishPreviewVideo.src = videoUrl;
          }
          if (publishPreviewLabel) {
            publishPreviewLabel.innerHTML = `Đang chọn: <code>${escapeHtml(selectedOpt.value)}</code>`;
          }
          if (publishGeneralTitle) {
            publishGeneralTitle.value = videoTitle;
          }
          const savedPosts = parseSocialPostsDataset(selectedOpt.dataset.socialPosts);
          if (savedPosts && applySocialPostsToForm(savedPosts, videoTitle)) {
            showPublishStatus(`✨ Đã tự động điền bộ Caption & Hashtag riêng của "${videoTitle}" (do Gemini AI tạo).`, false);
          } else {
            generateCaptionsWithGemini();
          }
        });
      }

      function updateVideoInventoryUI(videos, selectedVideoName = null) {
        if (!Array.isArray(videos)) return;
        const countPill = document.getElementById("video-library-count-pill");
        if (countPill) countPill.textContent = `${videos.length} video`;

        if (publishVideoSelect) {
          const currentVal = selectedVideoName || publishVideoSelect.value;
          publishVideoSelect.innerHTML = '<option value="">-- Chọn video trong storage\\downloads --</option>';
          videos.forEach((v) => {
            const opt = document.createElement("option");
            opt.value = v.name;
            opt.dataset.videoUrl = `/videos/${encodeURIComponent(v.name)}`;
            opt.dataset.videoTitle = v.title;
            opt.dataset.socialPosts = JSON.stringify(v.social_posts || {});
            const aiTag = v.social_posts ? " ✨ Có sẵn Caption AI" : "";
            opt.textContent = `${v.name} (${v.size_mb} MB · ${v.modified})${aiTag}`;
            if (v.name === currentVal) opt.selected = true;
            publishVideoSelect.append(opt);
          });
        }

        const grid = document.getElementById("video-library-grid");
        const emptyMsg = document.getElementById("video-library-empty");
        if (grid) {
          if (videos.length === 0) {
            grid.hidden = true;
            if (emptyMsg) emptyMsg.hidden = false;
          } else {
            grid.hidden = false;
            if (emptyMsg) emptyMsg.hidden = true;
            grid.innerHTML = videos
              .map((v) => {
                const vUrl = `/videos/${encodeURIComponent(v.name)}`;
                const socialPostsAttr = escapeHtml(JSON.stringify(v.social_posts || {}));
                return `
                  <article class="video-card" data-video-card="${escapeHtml(v.name)}">
                    <video controls preload="metadata" src="${escapeHtml(vUrl)}"></video>
                    <h3>${escapeHtml(v.title)}</h3>
                    <p class="video-meta muted">${escapeHtml(v.size_mb)} MB · ${escapeHtml(v.modified)}</p>
                    <div class="actions">
                      <button
                        type="button"
                        class="tiny js-select-video-for-publish"
                        data-video-name="${escapeHtml(v.name)}"
                        data-video-title="${escapeHtml(v.title)}"
                        data-video-url="${escapeHtml(vUrl)}"
                        data-social-posts="${socialPostsAttr}"
                      >
                        🚀 Chọn đăng
                      </button>
                      <a class="button secondary tiny" href="${escapeHtml(vUrl)}" download>Tải xuống</a>
                    </div>
                  </article>
                `;
              })
              .join("");
          }
        }
      }

      if (importVideoOnlyBtn && publishForm) {
        importVideoOnlyBtn.addEventListener("click", triggerImportAndAutoGemini);
      }

      if (generateGeminiCaptionsBtn) {
        generateGeminiCaptionsBtn.addEventListener("click", generateCaptionsWithGemini);
      }

      if (applyDraftAutofillBtn && draftAutofillSelect) {
        applyDraftAutofillBtn.addEventListener("click", () => {
          const opt = draftAutofillSelect.options[draftAutofillSelect.selectedIndex];
          if (!opt || !opt.value) {
            showPublishStatus("Hãy chọn một kịch bản trong danh sách để lấy gợi ý.", true);
            return;
          }
          const title = opt.dataset.title || "";
          const savedPosts = parseSocialPostsDataset(opt.dataset.socialPosts);
          if (savedPosts && applySocialPostsToForm(savedPosts, title)) {
            showPublishStatus(`✅ Đã điền bộ Caption & Hashtag riêng từ kịch bản "${title}" vào cả 3 nền tảng.`, false);
            return;
          }
          const desc = opt.dataset.description || "";
          const rawTags = opt.dataset.tags ? `#${opt.dataset.tags}` : "#xuhuong #haihuoc";
          if (publishGeneralTitle && title) publishGeneralTitle.value = title;

          ["tiktok", "facebook", "youtube"].forEach((platform) => {
            const tEl = document.getElementById(`${platform}-title`);
            const cEl = document.getElementById(`${platform}-caption`);
            const hEl = document.getElementById(`${platform}-hashtags`);
            const platformTags =
              platform === "youtube" && !rawTags.toLowerCase().includes("#shorts")
                ? `#Shorts ${rawTags}`
                : rawTags;
            if (hEl && rawTags) {
              hEl.value = platformTags;
            }
            if (tEl && title) {
              tEl.value =
                platform === "youtube"
                  ? buildYoutubeShortsTitle(title, platformTags, 100)
                  : title;
            }
            if (cEl && desc) cEl.value = desc;
          });
          showPublishStatus(`✅ Đã điền nội dung từ kịch bản "${title}" vào cả 3 nền tảng.`, false);
        });
      }

      if (copyTiktokToAllBtn) {
        copyTiktokToAllBtn.addEventListener("click", () => {
          const ttTitle = document.getElementById("tiktok-title")?.value || "";
          const ttCaption = document.getElementById("tiktok-caption")?.value || "";
          const ttHashtags = document.getElementById("tiktok-hashtags")?.value || "";

          ["facebook", "youtube"].forEach((platform) => {
            const tEl = document.getElementById(`${platform}-title`);
            const cEl = document.getElementById(`${platform}-caption`);
            const hEl = document.getElementById(`${platform}-hashtags`);
            const platformTags =
              platform === "youtube" && !ttHashtags.toLowerCase().includes("#shorts")
                ? `#Shorts ${ttHashtags}`.trim()
                : ttHashtags;
            if (hEl && ttHashtags) {
              hEl.value = platformTags;
            }
            if (tEl && ttTitle) {
              tEl.value =
                platform === "youtube"
                  ? buildYoutubeShortsTitle(ttTitle, platformTags, 100)
                  : ttTitle.slice(0, 150);
            }
            if (cEl && ttCaption) cEl.value = ttCaption;
          });
          if (publishGeneralTitle && !publishGeneralTitle.value.trim() && ttTitle) {
            publishGeneralTitle.value = ttTitle;
          }
          showPublishStatus("📋 Đã sao chép tiêu đề, caption và hashtag từ TikTok sang Facebook Reels và YouTube Shorts (kèm hashtag vào Tiêu đề YouTube Shorts).", false);
        });
      }

      const ytHashtagsInput = document.getElementById("youtube-hashtags");
      const ytTitleInput = document.getElementById("youtube-title");
      if (ytHashtagsInput && ytTitleInput) {
        ytHashtagsInput.addEventListener("change", () => {
          const baseTitle = ytTitleInput.value || publishGeneralTitle?.value || "";
          if (baseTitle.trim()) {
            ytTitleInput.value = buildYoutubeShortsTitle(baseTitle, ytHashtagsInput.value, 100);
          }
        });
      }

      document.addEventListener("click", (event) => {
        const openStudioBtn = event.target.closest(".js-open-publish-studio");
        if (openStudioBtn) {
          activateDashboardPage("videos", true);
          const studio = document.getElementById("multiplatform-studio-card");
          if (studio) studio.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }

        const selectVideoBtn = event.target.closest(".js-select-video-for-publish");
        if (selectVideoBtn) {
          const videoName = selectVideoBtn.dataset.videoName || "";
          const videoTitle = selectVideoBtn.dataset.videoTitle || "";
          const videoUrl = selectVideoBtn.dataset.videoUrl || "";
          const savedPosts = parseSocialPostsDataset(selectVideoBtn.dataset.socialPosts);
          activateDashboardPage("videos", true);
          if (publishFileInput) publishFileInput.value = "";
          if (publishLocalPathInput) publishLocalPathInput.value = "";
          if (publishVideoSelect) publishVideoSelect.value = videoName;
          if (publishGeneralTitle) publishGeneralTitle.value = videoTitle;
          if (publishPreviewVideo && videoUrl) publishPreviewVideo.src = videoUrl;
          if (publishPreviewLabel) {
            publishPreviewLabel.innerHTML = `Đang chọn: <code>${escapeHtml(videoName)}</code>`;
          }
          const studio = document.getElementById("multiplatform-studio-card");
          if (studio) studio.scrollIntoView({ behavior: "smooth", block: "start" });
          if (savedPosts && applySocialPostsToForm(savedPosts, videoTitle)) {
            showPublishStatus(`🎬 Đã chọn video "${videoName}" và tự động điền bộ Caption & Hashtag riêng (do Gemini AI tạo).`, false);
          } else {
            generateCaptionsWithGemini();
          }
        }
      });

      if (publishForm) {
        publishForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          const formData = new FormData(publishForm);
          if (publishSubmitBtn) publishSubmitBtn.disabled = true;
          showPublishStatus("🚀 Đang tải và đăng video lên các nền tảng đã chọn (TikTok / Facebook Reels / YouTube Shorts)… Vui lòng chờ trong giây lát.", false);
          try {
            const response = await fetch(publishForm.action, {
              method: "POST",
              body: formData
            });
            const result = await safeParseJson(response, "Đăng video thất bại.");

            if (result.videos) {
              updateVideoInventoryUI(result.videos, result.report?.video_name);
            }
            if (publishFileInput) publishFileInput.value = "";
            if (publishLocalPathInput) publishLocalPathInput.value = "";

            const statuses = result.report?.statuses || {};
            const labels = [
              ["tiktok", "🎵 TikTok"],
              ["facebook", "📘 Facebook Reels"],
              ["youtube", "▶️ YouTube Shorts"]
            ];
            const statusItemsHtml = labels
              .map(([key, label]) => {
                const st = statuses[key] || {};
                if (st.skipped) {
                  return `<div>⏭️ <strong>${label}:</strong> <span class="muted">Bỏ qua (không chọn đăng)</span></div>`;
                }
                if (st.success) {
                  const linkHtml = st.url && st.url.startsWith("https://")
                    ? ` · <a href="${escapeHtml(st.url)}" target="_blank" rel="noopener noreferrer" style="color:#86efac;">Xem bài đăng ↗</a>`
                    : (st.message ? ` · <span>${escapeHtml(st.message)}</span>` : "");
                  return `<div>✅ <strong>${label}:</strong> <span class="ok">Thành công</span>${linkHtml}</div>`;
                }
                return `<div>❌ <strong>${label}:</strong> <span class="bad">${escapeHtml(st.error || "Lỗi không xác định")}</span></div>`;
              })
              .join("");

            const anyFailed = labels.some(([key]) => {
              const st = statuses[key] || {};
              return !st.skipped && !st.success;
            });

            showPublishStatus(
              `<div style="font-weight:700; margin-bottom:6px;">${escapeHtml(result.message)}</div>${statusItemsHtml}`,
              anyFailed,
              true
            );
          } catch (error) {
            showPublishStatus(error.message || "Có lỗi xảy ra khi đăng video.", true);
          } finally {
            if (publishSubmitBtn) publishSubmitBtn.disabled = false;
          }
        });
      }

      render();
      refreshProcessStatus();
      window.setInterval(refreshProcessStatus, 2500);
      window.addEventListener("resize", drawConnections);
    })();
