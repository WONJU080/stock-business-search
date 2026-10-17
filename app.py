import queue
import threading
import tkinter as tk
from collections import Counter
from tkinter import messagebox, scrolledtext, ttk

import batch
from tools import db
from tools.analyze_flow import analyze
from tools.config import load_config, load_keys, save_config
from tools.llm import build_llm
from tools.search_flow import search


class StockApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.search_results = []
        self.stop_event = threading.Event()
        self.running = None
        root.title("股票业务分析检索")
        root.geometry("960x720")

        self._build_notebook()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_config_to_form()
        self._refresh_list()
        self.root.after(100, self._poll)

    # ---------- 界面 ----------

    def _build_notebook(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        nb.add(self._build_analyze_tab(nb), text="分析入库")
        nb.add(self._build_search_tab(nb), text="检索")
        nb.add(self._build_list_tab(nb), text="已分析股票")
        nb.add(self._build_batch_tab(nb), text="批量分析")
        nb.add(self._build_config_tab(nb), text="API 设置")

    def _build_analyze_tab(self, parent):
        f = ttk.Frame(parent, padding=10)

        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="范围:").pack(side="left")
        self.analyze_spec = ttk.Entry(r1)
        self.analyze_spec.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Label(
            f, text="例: 科大讯飞 / 600519 / 全A股 / 科创50 / 指数:沪深300",
            foreground="gray",
        ).pack(anchor="w")

        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=4)
        ttk.Label(r2, text="报告期:").pack(side="left")
        self.analyze_report = ttk.Entry(r2, width=12)
        self.analyze_report.pack(side="left", padx=5)
        ttk.Label(r2, text="(留空=自动最新年报)", foreground="gray").pack(side="left", padx=5)
        self.analyze_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="强制重新分析", variable=self.analyze_force).pack(side="left", padx=10)

        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=6)
        self.analyze_btn = ttk.Button(r3, text="开始分析", command=self._start_analyze)
        self.analyze_btn.pack(side="left")
        self.analyze_stop_btn = ttk.Button(r3, text="停止", command=self._stop_run, state="disabled")
        self.analyze_stop_btn.pack(side="left", padx=6)

        self.analyze_log = scrolledtext.ScrolledText(f, height=26, state="disabled")
        self.analyze_log.pack(fill="both", expand=True, pady=(6, 0))
        return f

    def _build_search_tab(self, parent):
        f = ttk.Frame(parent, padding=10)

        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="搜索词:").pack(side="left")
        self.search_query = ttk.Entry(r1)
        self.search_query.pack(side="left", fill="x", expand=True, padx=5)
        self.search_btn = ttk.Button(r1, text="搜索", command=self._start_search)
        self.search_btn.pack(side="left", padx=5)
        self.search_query.bind("<Return>", lambda e: self._start_search())

        self.search_status = ttk.Label(f, text="就绪", foreground="gray")
        self.search_status.pack(anchor="w", pady=(4, 0))

        cols = ("code", "name", "score", "matched")
        self.search_tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        for c, t, w in [("code", "代码", 80), ("name", "名称", 140), ("score", "关联度", 80), ("matched", "命中词", 240)]:
            self.search_tree.heading(c, text=t)
            self.search_tree.column(c, width=w)
        self.search_tree.pack(fill="both", expand=True, pady=(6, 0))
        self.search_tree.bind("<<TreeviewSelect>>", self._on_search_select)

        self.search_detail = scrolledtext.Text(f, height=9, state="disabled", wrap="word")
        self.search_detail.pack(fill="x", pady=(6, 0))
        return f

    def _build_list_tab(self, parent):
        f = ttk.Frame(parent, padding=10)
        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=4)
        ttk.Button(r1, text="刷新", command=self._refresh_list).pack(side="left")
        self.list_count = ttk.Label(r1, text="")
        self.list_count.pack(side="left", padx=10)

        cols = ("code", "name", "period", "analyzed", "updated")
        self.list_tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        for c, t, w in [("code", "代码", 80), ("name", "名称", 160), ("period", "报告期", 100), ("analyzed", "分析日期", 100), ("updated", "更新时间", 100)]:
            self.list_tree.heading(c, text=t)
            self.list_tree.column(c, width=w)
        self.list_tree.pack(fill="both", expand=False, pady=(6, 0))
        self.list_tree.bind("<<TreeviewSelect>>", self._on_list_select)

        self.list_detail = scrolledtext.Text(f, height=14, state="disabled", wrap="word")
        self.list_detail.pack(fill="both", expand=True, pady=(6, 0))
        return f

    def _build_batch_tab(self, parent):
        f = ttk.Frame(parent, padding=10)

        api = ttk.LabelFrame(f, text="API 配置", padding=8)
        api.pack(fill="x")

        self.batch_base = tk.StringVar()
        self.batch_model = tk.StringVar()
        r1 = ttk.Frame(api)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="base_url", width=10).pack(side="left")
        ttk.Entry(r1, textvariable=self.batch_base).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Label(r1, text="model", width=6).pack(side="left")
        ttk.Entry(r1, textvariable=self.batch_model, width=20).pack(side="left", padx=5)

        r2 = ttk.Frame(api)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="API key 池", width=10).pack(side="left", anchor="n")
        kr = ttk.Frame(r2)
        kr.pack(side="left", fill="x", expand=True)
        self.batch_keys_list = tk.Listbox(kr, height=4)
        self.batch_keys_list.pack(fill="x")
        kr2 = ttk.Frame(kr)
        kr2.pack(fill="x", pady=(4, 0))
        self.batch_key_entry = ttk.Entry(kr2)
        self.batch_key_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(kr2, text="添加", command=self._add_batch_key).pack(side="left", padx=4)
        ttk.Button(kr2, text="删除选中", command=self._del_batch_key).pack(side="left")

        prm = ttk.LabelFrame(f, text="分析参数", padding=8)
        prm.pack(fill="x", pady=(8, 0))

        def param(row_idx, label, var, width=14):
            fr = ttk.Frame(prm)
            fr.grid(row=row_idx, column=0, sticky="w", pady=3)
            ttk.Label(fr, text=label, width=12).pack(side="left")
            ttk.Entry(fr, textvariable=var, width=width).pack(side="left")

        self.batch_spec = tk.StringVar(value="全A股")
        self.batch_offset = tk.StringVar(value="0")
        self.batch_limit = tk.StringVar(value="500")
        self.batch_workers = tk.StringVar(value="2")
        self.batch_force = tk.BooleanVar(value=False)
        param(0, "范围", self.batch_spec, 16)
        param(1, "offset", self.batch_offset, 8)
        param(2, "limit", self.batch_limit, 8)
        param(3, "每key并发", self.batch_workers, 8)
        ttk.Checkbutton(prm, text="强制重新分析", variable=self.batch_force).grid(row=4, column=0, sticky="w", pady=3)
        ttk.Label(prm, text="范围示例: 全A股 / 科创50 / 600519", foreground="gray").grid(row=5, column=0, sticky="w")

        mid = ttk.Frame(f)
        mid.pack(fill="x", pady=(8, 0))
        self.batch_btn = ttk.Button(mid, text="开始批量分析", command=self._start_batch)
        self.batch_btn.pack(side="left")
        self.batch_stop_btn = ttk.Button(mid, text="停止", command=self._stop_run, state="disabled")
        self.batch_stop_btn.pack(side="left", padx=6)
        self.batch_status = ttk.Label(mid, text="", foreground="gray")
        self.batch_status.pack(side="left", padx=10)

        self.batch_log = scrolledtext.ScrolledText(f, height=18, state="disabled")
        self.batch_log.pack(fill="both", expand=True, pady=(6, 0))

        self._load_batch_config()
        return f

    def _mask_key(self, k):
        if len(k) <= 10:
            return "***"
        return f"{k[:6]}...{k[-4:]}"

    def _load_batch_config(self):
        llm = load_config()["llm"]
        self.batch_base.set(llm.get("base_url", ""))
        self.batch_model.set(llm.get("model", ""))
        self.batch_keys = list(load_keys())
        self._render_batch_keys()

    def _render_batch_keys(self):
        lb = self.batch_keys_list
        lb.delete(0, "end")
        for k in self.batch_keys:
            lb.insert("end", self._mask_key(k))

    def _add_batch_key(self):
        k = self.batch_key_entry.get().strip()
        if not k:
            return
        if k not in self.batch_keys:
            self.batch_keys.append(k)
        self.batch_key_entry.delete(0, "end")
        self._render_batch_keys()

    def _del_batch_key(self):
        sel = self.batch_keys_list.curselection()
        if not sel:
            return
        del self.batch_keys[sel[0]]
        self._render_batch_keys()

    def _save_batch_config(self):
        cfg = load_config()
        cfg["llm"]["base_url"] = self.batch_base.get().strip()
        cfg["llm"]["model"] = self.batch_model.get().strip()
        cfg["llm"]["api_keys"] = list(self.batch_keys)
        if not cfg["llm"].get("api_key") and self.batch_keys:
            cfg["llm"]["api_key"] = self.batch_keys[0]
        save_config(cfg)

    def _start_batch(self):
        spec = self.batch_spec.get().strip()
        if not spec:
            messagebox.showwarning("提示", "请输入范围")
            return
        if self.running:
            messagebox.showwarning("提示", "已有分析任务在运行，请先停止或等待完成")
            return
        self._save_batch_config()

        if not load_keys():
            messagebox.showwarning("提示", "请先添加至少一个 API key")
            return
        try:
            offset = int(self.batch_offset.get().strip() or 0)
            limit = int(self.batch_limit.get().strip()) if self.batch_limit.get().strip() else None
            workers = int(self.batch_workers.get().strip() or 2)
        except ValueError:
            messagebox.showwarning("提示", "offset/limit/并发 必须为整数")
            return
        force = self.batch_force.get()
        self.stop_event.clear()
        self.running = "batch"
        self.batch_btn.config(state="disabled")
        self.analyze_btn.config(state="disabled")
        self.batch_stop_btn.config(state="normal")
        self.batch_status.config(text="正在分析...", foreground="blue")
        self._log_batch(f"开始批量分析: {spec} (offset={offset} limit={limit})\n")

        def worker():
            try:
                def on_progress(item):
                    self.q.put(("batch_progress", item))
                results = batch.run(
                    spec, offset=offset, limit=limit,
                    workers_per_key=workers, force=force, on_progress=on_progress,
                    stop_event=self.stop_event,
                )
                self.q.put(("batch_done", results))
            except Exception as e:
                self.q.put(("batch_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _log_batch(self, text):
        self.batch_log.config(state="normal")
        self.batch_log.insert("end", text)
        self.batch_log.see("end")
        self.batch_log.config(state="disabled")

    def _build_config_tab(self, parent):
        f = ttk.Frame(parent, padding=24)
        ttk.Label(f, text="OpenAI 兼容 API 配置（DeepSeek / OpenAI / Ollama 等）").pack(anchor="w", pady=(0, 10))

        self.cfg_base = tk.StringVar()
        self.cfg_key = tk.StringVar()
        self.cfg_model = tk.StringVar()

        def row(label, var, show=None):
            fr = ttk.Frame(f)
            fr.pack(fill="x", pady=6)
            ttk.Label(fr, text=label, width=12).pack(side="left")
            e = ttk.Entry(fr, textvariable=var, show=show)
            e.pack(side="left", fill="x", expand=True)
            return e

        row("base_url", self.cfg_base)
        row("api_key", self.cfg_key, show="*")
        row("model", self.cfg_model)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=12)
        ttk.Button(btns, text="保存配置", command=self._save_config).pack(side="left")
        ttk.Button(btns, text="重新加载", command=self._load_config_to_form).pack(side="left", padx=8)

        self.cfg_status = ttk.Label(f, text="", foreground="green")
        self.cfg_status.pack(anchor="w")
        return f

    # ---------- 分析 ----------

    def _start_analyze(self):
        spec = self.analyze_spec.get().strip()
        if not spec:
            messagebox.showwarning("提示", "请输入范围")
            return
        if self.running:
            messagebox.showwarning("提示", "已有分析任务在运行，请先停止或等待完成")
            return
        if not load_keys():
            messagebox.showwarning("提示", "尚未配置 API key，请先在「API 设置」页填写")
            return
        report = self.analyze_report.get().strip() or "-"
        force = self.analyze_force.get()
        self.stop_event.clear()
        self.running = "analyze"
        self.analyze_btn.config(state="disabled")
        self.analyze_stop_btn.config(state="normal")
        self.batch_btn.config(state="disabled")
        self._log_analyze(f"开始分析: {spec}\n")

        def worker():
            try:
                def on_progress(item):
                    self.q.put(("analyze_progress", item))
                results = analyze(
                    spec, report_period=report, llm=build_llm(),
                    force=force, on_progress=on_progress,
                    stop_event=self.stop_event,
                )
                self.q.put(("analyze_done", results))
            except Exception as e:
                self.q.put(("analyze_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _stop_run(self):
        if not self.running:
            return
        self.stop_event.set()
        msg = "正在停止（等待当前股票完成）...\n"
        if self.running == "batch":
            self._log_batch(msg)
            self.batch_status.config(text="正在停止...", foreground="orange")
        else:
            self._log_analyze(msg)

    # ---------- 检索 ----------

    def _start_search(self):
        q = self.search_query.get().strip()
        if not q:
            return
        self.search_btn.config(state="disabled")
        self.search_status.config(text="正在搜索...", foreground="blue")
        self.search_results = []
        for i in self.search_tree.get_children():
            self.search_tree.delete(i)
        self.search_detail.config(state="normal")
        self.search_detail.delete("1.0", "end")
        self.search_detail.config(state="disabled")

        def worker():
            try:
                res = search(q, llm=build_llm())
                self.q.put(("search_done", res))
            except Exception as e:
                self.q.put(("search_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_select(self, event):
        sel = self.search_tree.selection()
        if not sel:
            return
        idx = self.search_tree.index(sel[0])
        if idx >= len(self.search_results):
            return
        r = self.search_results[idx]
        self.search_detail.config(state="normal")
        self.search_detail.delete("1.0", "end")
        self.search_detail.insert("end", f"{r['code']}  {r.get('name', '')}\n\n{r.get('business', '')}")
        self.search_detail.config(state="disabled")

    # ---------- 配置 ----------

    def _load_config_to_form(self):
        llm = load_config()["llm"]
        self.cfg_base.set(llm.get("base_url", ""))
        self.cfg_key.set(llm.get("api_key", ""))
        self.cfg_model.set(llm.get("model", ""))

    def _save_config(self):
        cfg = load_config()
        cfg["llm"]["base_url"] = self.cfg_base.get().strip()
        key = self.cfg_key.get().strip()
        cfg["llm"]["api_key"] = key
        cfg["llm"]["model"] = self.cfg_model.get().strip()
        if key and not cfg["llm"].get("api_keys"):
            cfg["llm"]["api_keys"] = [key]
        save_config(cfg)
        self.cfg_status.config(text="已保存到 config.json")
        self._load_batch_config()

    # ---------- 工具 ----------

    def _refresh_list(self):
        tree = self.list_tree
        for i in tree.get_children():
            tree.delete(i)
        records = db.list_analyzed()
        for r in records:
            tree.insert("", "end", values=(r["code"], r["name"], r["report_period"], r["analyzed_at"], r["updated_at"]))
        self.list_count.config(text=f"共 {len(records)} 只")

    def _on_list_select(self, event):
        sel = self.list_tree.selection()
        if not sel:
            return
        code = str(self.list_tree.item(sel[0])["values"][0]).zfill(6)
        try:
            data = db.parse_stock_file(code)
        except Exception as e:
            self.list_detail.config(state="normal")
            self.list_detail.delete("1.0", "end")
            self.list_detail.insert("end", f"读取失败: {e}")
            self.list_detail.config(state="disabled")
            return
        text = f"{data['code']}  {data['name']}\n"
        text += f"报告期: {data['report_period']}   分析: {data['analyzed_at']}   更新: {data['updated_at']}\n"
        if data["keywords"]:
            text += f"关键词: {', '.join(data['keywords'])}\n"
        text += "\n" + data["business"]
        self.list_detail.config(state="normal")
        self.list_detail.delete("1.0", "end")
        self.list_detail.insert("end", text)
        self.list_detail.config(state="disabled")

    def _log_analyze(self, text):
        self.analyze_log.config(state="normal")
        self.analyze_log.insert("end", text)
        self.analyze_log.see("end")
        self.analyze_log.config(state="disabled")

    def _finish_run(self):
        self.running = None
        self.stop_event.clear()
        self.analyze_btn.config(state="normal")
        self.batch_btn.config(state="normal")
        self.analyze_stop_btn.config(state="disabled")
        self.batch_stop_btn.config(state="disabled")

    def _on_close(self):
        self.stop_event.set()
        self.root.destroy()

    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "analyze_progress":
            item = msg[1]
            line = f"{item['code']}\t{item.get('name', '')}\t{item['status']}"
            if item["status"] == "error":
                line += f"\t{item.get('stage', '')}: {item.get('error', '')}"
            self._log_analyze(line + "\n")
        elif kind == "analyze_done":
            results = msg[1]
            c = Counter(r["status"] for r in results)
            self._log_analyze(f"\n完成: {dict(c)}\n")
            self._finish_run()
            self._refresh_list()
        elif kind == "analyze_error":
            self._log_analyze(f"错误: {msg[1]}\n")
            self._finish_run()
        elif kind == "search_done":
            res = msg[1]
            self.search_results = res["results"]
            tree = self.search_tree
            for i in tree.get_children():
                tree.delete(i)
            for r in res["results"]:
                tree.insert("", "end", values=(r["code"], r.get("name", ""), r.get("score", "-"), ",".join(r.get("matched", []))))
            related = " / ".join(res.get("related", []))
            n = len(res["results"])
            total = res.get("total_hits", n)
            warn = "；".join(res.get("warnings", []))
            suffix = f"   {warn}" if warn else ""
            if n:
                self.search_status.config(
                    text=f"命中 {total} 只，展示 {n} 只   关联词: {related}{suffix}",
                    foreground="green" if not warn else "orange",
                )
            else:
                self.search_status.config(
                    text=f"无符合条件的股票   已尝试关联词: {related}{suffix}", foreground="orange"
                )
            self.search_btn.config(state="normal")
        elif kind == "search_error":
            self.search_status.config(text=f"检索失败: {msg[1]}", foreground="red")
            messagebox.showerror("检索失败", msg[1])
            self.search_btn.config(state="normal")
        elif kind == "batch_progress":
            item = msg[1]
            line = f"{item['code']}\t{item.get('name', '')}\t{item['status']}"
            if item["status"] == "error":
                line += f"\t{item.get('stage', '')}: {item.get('error', '')}"
            self._log_batch(line + "\n")
        elif kind == "batch_done":
            results = msg[1]
            c = Counter(r["status"] for r in results)
            self._log_batch(f"\n完成: {dict(c)}\n")
            self.batch_status.config(text=f"完成: {dict(c)}", foreground="green")
            self._finish_run()
            self._refresh_list()
        elif kind == "batch_error":
            self._log_batch(f"错误: {msg[1]}\n")
            self.batch_status.config(text=f"失败: {msg[1]}", foreground="red")
            self._finish_run()


def main():
    root = tk.Tk()
    StockApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
