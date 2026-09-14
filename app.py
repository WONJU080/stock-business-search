import queue
import threading
import tkinter as tk
from collections import Counter
from tkinter import messagebox, scrolledtext, ttk

from tools import db
from tools.analyze_flow import analyze
from tools.config import load_config, save_config
from tools.llm import build_llm
from tools.search_flow import search


class StockApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.search_results = []
        root.title("股票业务分析检索")
        root.geometry("960x720")

        self._build_notebook()
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
        if not load_config()["llm"].get("api_key"):
            messagebox.showwarning("提示", "尚未配置 API key，请先在「API 设置」页填写")
            return
        report = self.analyze_report.get().strip() or "-"
        force = self.analyze_force.get()
        self.analyze_btn.config(state="disabled")
        self._log_analyze(f"开始分析: {spec}\n")

        def worker():
            try:
                def on_progress(item):
                    self.q.put(("analyze_progress", item))
                results = analyze(
                    spec, report_period=report, llm=build_llm(),
                    force=force, on_progress=on_progress,
                )
                self.q.put(("analyze_done", results))
            except Exception as e:
                self.q.put(("analyze_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

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
        cfg["llm"]["api_key"] = self.cfg_key.get().strip()
        cfg["llm"]["model"] = self.cfg_model.get().strip()
        save_config(cfg)
        self.cfg_status.config(text="已保存到 config.json")

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
                line += f"\t{item.get('error', '')}"
            self._log_analyze(line + "\n")
        elif kind == "analyze_done":
            results = msg[1]
            c = Counter(r["status"] for r in results)
            self._log_analyze(f"\n完成: {dict(c)}\n")
            self.analyze_btn.config(state="normal")
            self._refresh_list()
        elif kind == "analyze_error":
            self._log_analyze(f"错误: {msg[1]}\n")
            self.analyze_btn.config(state="normal")
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
            if n:
                self.search_status.config(
                    text=f"找到 {n} 只股票   关联词: {related}", foreground="green"
                )
            else:
                self.search_status.config(
                    text=f"无符合条件的股票   已尝试关联词: {related}", foreground="orange"
                )
            self.search_btn.config(state="normal")
        elif kind == "search_error":
            self.search_status.config(text=f"检索失败: {msg[1]}", foreground="red")
            messagebox.showerror("检索失败", msg[1])
            self.search_btn.config(state="normal")


def main():
    root = tk.Tk()
    StockApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
