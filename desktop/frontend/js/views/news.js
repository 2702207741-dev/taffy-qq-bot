/* views/news.js — 资讯（6.6）：官方/社区统一 Tabs（含未读角标）/ 正文排版 /
   外链统一样式（下划线+↗）；5 分钟自动刷新提示放页头。 */

import { get, post } from "../api.js";
import { store } from "../store.js";
import { toast, fmtTime } from "../ui.js";

export default {
  data() {
    return {
      tab: "official",
      official: null,
      officialLoading: false,
      reddit: null,
      redditLoading: false,
      unreadOnly: true,
    };
  },
  computed: {
    updates() {
      return (this.official && this.official.updates) || [];
    },
    upcoming() {
      return (this.official && this.official.upcoming) || [];
    },
    completed() {
      return (this.official && this.official.completed) || [];
    },
    posts() {
      return (this.reddit && this.reddit.posts) || [];
    },
    unreadCount() {
      const all = (this.reddit && this.reddit.posts) || [];
      const unread = all.filter((p) => p.unread).length;
      if (this.unreadOnly) return unread;
      return this.reddit ? this.reddit.unread_total ?? unread : 0;
    },
    matchColumns() {
      return [
        { key: "time", label: "时间", width: "150px" },
        { key: "league", label: "赛事", minWidth: "140px" },
        {
          key: "_match",
          label: "对阵",
          render: (row) =>
            row.score_a != null
              ? `${row.team_a} ${row.score_a}:${row.score_b} ${row.team_b}`
              : `${row.team_a} vs ${row.team_b}`,
        },
      ];
    },
    matchRows() {
      return [...this.upcoming, ...this.completed];
    },
  },
  mounted() {
    this.loadOfficial();
    this.loadReddit();
    this.timer = setInterval(() => {
      this.loadOfficial();
      this.loadReddit();
      this.checkNewPosts(); // 通知规则中心：新爆料桌面通知
    }, 5 * 60 * 1000);
    this.onRefresh = () => {
      this.loadOfficial();
      this.loadReddit();
    };
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    clearInterval(this.timer);
    window.removeEventListener("view-refresh", this.onRefresh);
  },
  methods: {
    fmtTime,
    async openExternal(url) {
      if (!url) return;
      try {
        const result = await window.pywebview.api.open_external(url);
        if (result !== "ok") toast("仅允许 https:// 链接在外部浏览器打开", "error");
      } catch {
        toast("外部打开失败", "error");
      }
    },
    async loadOfficial() {
      this.officialLoading = !this.official;
      const res = await get("/api/desktop/news/official");
      this.official = res.status === 200 && res.body.ok ? res.body.data : null;
      this.officialLoading = false;
    },
    async loadReddit() {
      this.redditLoading = !this.reddit;
      const res = await get(`/api/desktop/news/reddit?unread=${this.unreadOnly ? 1 : 0}`);
      this.reddit = res.status === 200 && res.body.ok ? res.body.data : null;
      this.redditLoading = false;
    },
    async markRead(post) {
      const res = await post("/api/desktop/news/reddit/read", { post_ids: [post.post_id] });
      if (res.status === 200 && res.body.ok && res.body.data.marked > 0) {
        toast("已标记为已读", "success");
        this.loadReddit();
      }
    },
    async checkNewPosts() {
      const rules = store.settings.notify_rules || {};
      const rule = rules.new_reddit || {};
      if (!rule.toast) return; // 规则未开启则静默
      /* 新帖检测由后台轮询比对 first_seen_at；此处刷新即可触发通知中心 */
      window.dispatchEvent(new CustomEvent("notify-poll"));
    },
  },
  template: `
  <div>
    <page-header title="资讯"
                 subtitle="官方更新与赛事 · 社区爆料 —— 每 5 分钟自动刷新">
      <template #meta>
        <span v-if="tab==='official' && officialLoading" class="muted">加载中…</span>
      </template>
      <button class="btn sm ghost" @click="loadOfficial(); loadReddit()">立即刷新</button>
    </page-header>

    <app-tabs :tabs="[
      { key: 'official', label: '官方更新 / 赛事' },
      { key: 'reddit', label: '社区爆料', badge: unreadCount || '' },
    ]" v-model:active="tab" />

    <!-- ══ 官方 ══ -->
    <template v-if="tab==='official'">
      <state-panel v-if="officialLoading" state="loading" :rows="4" />
      <div v-else-if="official" class="card mb-3 prose-body">{{ official.text }}</div>

      <div class="grid-2">
        <div class="card">
          <h3>🎮 更新日志</h3>
          <article v-for="(u, i) in updates" :key="i" class="news-item">
            <div class="row wrap"><b>{{ u.title }}</b><span class="faint num">{{ u.time }}</span></div>
            <div class="muted mt-1">{{ u.summary }}</div>
            <a href="#" class="ext-link mt-1" @click.prevent="openExternal(u.url)">{{ u.url }} ↗</a>
          </article>
          <state-panel v-if="official && !updates.length" state="empty" icon="🎮"
                       title="暂无更新日志" message=" Valve 发布新版本时会自动出现在这里。" tight />
        </div>

        <div class="card">
          <h3>🏆 赛事赛程与赛果</h3>
          <data-table v-if="matchRows.length" :columns="matchColumns" :rows="matchRows" row-key="_match" />
          <state-panel v-else state="empty" icon="🏆" title="暂无赛事数据"
                       message="当前无通过校验的赛事信息。" tight />
        </div>
      </div>
    </template>

    <!-- ══ 爆料 ══ -->
    <template v-else>
      <div class="row mb-2">
        <label class="checkbox-row">
          <input type="checkbox" v-model="unreadOnly" @change="loadReddit()" /> 只看未读
        </label>
      </div>
      <div class="card">
        <state-panel v-if="redditLoading" state="loading" :rows="4" />
        <template v-else>
          <div v-for="p in posts" :key="p.post_id"
               class="news-row row-wrap-split"
               role="listitem">
            <div class="grow">
              <div class="news-title" :class="{ unread: p.unread }">{{ p.title }}
                <status-pill v-if="p.unread" label="未读" tone="accent" :dot="false" />
              </div>
              <div class="faint">r/{{ p.subreddit }} · {{ fmtTime(p.published_at) }} · {{ p.author || '匿名' }}</div>
              <div class="muted mt-1" v-if="p.summary">{{ p.summary }}</div>
            </div>
            <div class="row news-actions">
              <button v-if="p.unread" class="btn sm ghost" @click="markRead(p)">标已读</button>
              <button class="btn sm ghost" @click="openExternal(p.url)">打开 ↗</button>
            </div>
          </div>
          <state-panel v-if="!posts.length" state="empty" icon="📰"
                       :title="'没有' + (unreadOnly ? '未读' : '') + '爆料帖'"
                       message="r/GlobalOffensive 等社区的新帖子会在这里聚合。" />
        </template>
      </div>
    </template>
  </div>
  `,
};
