/* views/chat.js — 聊天（6.2）：气泡流 / 人格上下文条（StatusPill）/ 命令补全 /
   发送 loading 态；长回复 >600 字折叠「展开全文」（V2 可选项落地）。 */

import { get, post, del } from "../api.js";
import { toast, asText, confirmBox } from "../ui.js";

const COMMANDS = [
  { cmd: "记住", hint: "记住 <内容> — 写入长期记忆" },
  { cmd: "以后叫我", hint: "以后叫我 <昵称>" },
  { cmd: "忘掉记忆", hint: "忘掉记忆 <序号>" },
  { cmd: "大盘", hint: "市场大盘概览" },
  { cmd: "查询 ", hint: "查询 <饰品名> — 实时报价" },
  { cmd: "K线 ", hint: "K线 <饰品名> — 日K走势" },
  { cmd: "抄底", hint: "抄底·大盘历史情景" },
  { cmd: "预测", hint: "短期走势预测" },
];

const LONG_TEXT_LIMIT = 600;

export default {
  data() {
    return {
      messages: [],
      draft: "",
      sending: false,
      profile: null,
      lastElapsedMs: null,
      showCommands: false,
      commandFilter: "",
      sessionClearedAt: null,
    };
  },
  computed: {
    commandHits() {
      const q = this.commandFilter.trim();
      return COMMANDS.filter((c) => c.cmd.trim().startsWith(q) || q === "");
    },
    styleRadarPoints() {
      const s = this.profile ? this.profile.style : null;
      if (!s) return "";
      const dims = ["concise", "warm", "teasing", "proactive", "catchphrase", "direct"];
      return dims.map((d) => `${d}:${s[d] > 0 ? "+" : ""}${s[d]}`).join(" · ");
    },
  },
  created() {
    this.LONG_TEXT_LIMIT = LONG_TEXT_LIMIT;
  },
  mounted() {
    this.loadHistory();
    this.loadProfile();
    this.onRefresh = () => {
      this.loadHistory();
    };
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
  },
  methods: {
    async loadHistory() {
      const res = await get("/api/desktop/chat/history?limit=40");
      if (res.status === 200 && res.body.ok) {
        this.messages = res.body.data.messages.map((m) => ({
          role: m.role,
          text: asText(m.content),
          expanded: false,
        }));
        this.scrollBottom();
      }
    },
    async loadProfile() {
      const res = await get("/api/desktop/persona/profile");
      this.profile = res.status === 200 && res.body.ok ? res.body.data : null;
    },
    scrollBottom() {
      this.$nextTick(() => {
        const el = this.$refs.flow;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    onInput() {
      const value = this.draft;
      this.showCommands = value.startsWith("/");
      this.commandFilter = value.slice(1);
    },
    applyCommand(cmd) {
      this.draft = cmd.endsWith(" ") ? cmd : `${cmd} `;
      this.showCommands = false;
      this.$refs.input.focus();
    },
    toggleExpand(index) {
      this.messages[index].expanded = !this.messages[index].expanded;
    },
    async send() {
      const message = this.draft.trim();
      if (!message || this.sending) return;
      this.showCommands = false;
      this.draft = "";
      this.messages.push({ role: "user", text: asText(message), expanded: false });
      const pendingIndex = this.messages.push({ role: "assistant", text: "…", pending: true, expanded: false }) - 1;
      this.sending = true;
      this.scrollBottom();

      const started = Date.now();
      const res = await post("/api/desktop/chat", { message });
      const elapsed = Date.now() - started;

      if (res.status === 429) {
        // Q10：限流友好提示（chat 管线每会话 6/min，desktop 层 12/min）
        this.messages.splice(pendingIndex, 1);
        this.messages.push({
          role: "assistant",
          text: "聊得太快啦喵～桌面聊天与 QQ 共享每分钟额度，请稍等片刻再问。",
          expanded: false,
        });
      } else if (res.status === 200 && res.body.ok) {
        this.messages.splice(pendingIndex, 1, {
          role: "assistant",
          text: asText(res.body.data.reply),
          expanded: false,
        });
        this.lastElapsedMs = res.body.data.elapsed_ms ?? elapsed;
      } else {
        const reason = (res.body && res.body.message) || `请求失败（${res.status}）`;
        this.messages.splice(pendingIndex, 1, { role: "assistant", text: `⚠️ ${reason}`, expanded: false });
      }
      this.sending = false;
      this.scrollBottom();
      this.loadProfile(); // 记住/叫我等命令可能改变人格上下文
    },
    async clearHistory() {
      const okToClear = await confirmBox(
        "清空桌面聊天历史",
        "将删除桌面端与塔菲的会话记录（不影响 QQ 端私聊记忆）。",
        { danger: true }
      );
      if (!okToClear) return;
      const res = await del("/api/desktop/chat/history");
      if (res.status === 200) {
        this.messages = [];
        toast("桌面会话已清空", "success");
      } else {
        toast((res.body && res.body.message) || "清空失败", "error");
      }
    },
    async toggleLearning() {
      if (!this.profile) return;
      const target = !this.profile.learning_enabled;
      const okToggle = await confirmBox(
        target ? "开启风格学习" : "关闭风格学习",
        target ? "塔菲将根据你的反馈持续调整说话风格。" : "关闭后塔菲不再自动调整风格。"
      );
      if (!okToggle) return;
      const res = await post("/api/desktop/persona/style", {
        action: target ? "learning_on" : "learning_off",
      });
      if (res.status === 200) {
        toast(target ? "风格学习已开启" : "风格学习已关闭");
        this.loadProfile();
      }
    },
    async undoStyle() {
      const okUndo = await confirmBox("撤销最近一次风格调整", "回退上一条风格反馈的效果。");
      if (!okUndo) return;
      const res = await post("/api/desktop/persona/style", { action: "undo" });
      toast(
        res.status === 200 ? "已撤销上次调整" : (res.body && res.body.message) || "无可撤销记录",
        res.status === 200 ? "info" : "error"
      );
      if (res.status === 200) this.loadProfile();
    },
    async resetStyle() {
      const okReset = await confirmBox("重置风格档案", "六维风格将全部归零，此操作不可撤销。", { danger: true, requireType: "重置" });
      if (!okReset) return;
      const res = await post("/api/desktop/persona/style", { action: "reset" });
      if (res.status === 200) {
        toast("风格已重置为初始状态", "success");
        this.loadProfile();
      }
    },
  },
  template: `
  <div class="chat-shell">
    <page-header title="聊天"
                 subtitle="与塔菲对话 · 桌面与 QQ 私聊共享同一份记忆">
      <template #meta>
        <status-pill v-if="profile" :label="'学习' + (profile.learning_enabled ? '开' : '关')"
                     :tone="profile.learning_enabled ? 'ok' : 'neutral'" title="风格学习开关状态" />
      </template>
      <button class="btn sm ghost" @click="clearHistory">清空会话</button>
    </page-header>

    <!-- 人格上下文条 -->
    <div v-if="profile" class="card row wrap chat-context">
      <b>{{ profile.nickname ? '称呼：' + profile.nickname : '喵～' }}</b>
      <status-pill :label="'学习' + (profile.learning_enabled ? '开' : '关')"
                   :tone="profile.learning_enabled ? 'ok' : 'neutral'" title="风格学习开关状态" />
      <span class="muted grow ellipsis-one" :title="styleRadarPoints">{{ styleRadarPoints }}</span>
      <button class="btn sm ghost" @click="toggleLearning">{{ profile.learning_enabled ? '关闭学习' : '开启学习' }}</button>
      <button class="btn sm ghost" @click="undoStyle">撤销调整</button>
      <button class="btn sm ghost" @click="resetStyle">重置风格</button>
    </div>

    <div ref="flow" class="bubble-flow card grow-shell">
      <state-panel v-if="!messages.length" state="empty" icon="💬"
                   title="和塔菲说点什么吧～"
                   message="桌面与 QQ 私聊共享同一份记忆；输入 / 唤出命令补全。" />
      <div v-for="(m, i) in messages" :key="i" class="bubble" :class="[m.role, m.pending ? 'pending' : '']">{{
        m.expanded || m.text.length <= LONG_TEXT_LIMIT
          ? m.text
          : m.text.slice(0, LONG_TEXT_LIMIT) + ' ……'
      }}<button v-if="m.text.length > LONG_TEXT_LIMIT" class="linklike expand-btn"
           @click="toggleExpand(i)">{{ m.expanded ? '收起' : '展开全文' }}</button></div>
    </div>

    <div class="chat-input-bar">
      <div v-if="showCommands" class="card palette-list cmd-pop">
        <div v-for="c in commandHits" :key="c.cmd" class="palette-item" @click="applyCommand(c.cmd)">
          <b>{{ c.cmd }}</b><span class="muted">{{ c.hint }}</span>
        </div>
        <div v-if="!commandHits.length" class="faint cmd-empty">没有匹配的命令</div>
      </div>
      <textarea ref="input" v-model="draft" rows="2" class="grow" spellcheck="false"
                placeholder="输入消息，/ 唤出命令…（Enter 发送，Shift+Enter 换行）"
                @input="onInput"
                @keydown.enter.exact.prevent="send"></textarea>
      <button class="btn send-btn" :disabled="sending || !draft.trim()" @click="send">
        <span v-if="sending" class="spin" aria-hidden="true"></span>{{ sending ? '思考中' : '发送' }}
      </button>
    </div>
    <div v-if="lastElapsedMs != null" class="row mt-1">
      <span class="faint">回复用时 {{ (lastElapsedMs / 1000).toFixed(1) }}s · 桌面聊天与 QQ 共享每分钟额度</span>
    </div>
  </div>
  `,
};
