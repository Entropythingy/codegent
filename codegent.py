#!/usr/bin/env python3
import os, sys, json, time, subprocess, re, sqlite3, uuid
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime
import requests
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.key_binding import KeyBindings
try:
    import tomllib
except ImportError:
    import tomli as tomllib

HOME = Path.home()
CONFIG_DIR = HOME / '.config' / 'codegent'
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / 'config.toml'
HISTORY_FILE = CONFIG_DIR / 'history'
DB_FILE = CONFIG_DIR / 'sessions.db'
LOG_FILE = CONFIG_DIR / 'llama-server.log'

DEFAULT_CONFIG = {
    'llama_base': 'http://127.0.0.1:8081',
    'models_dir': str(HOME / 'models'),
    'llama_server': str(HOME / 'llama.cpp/build/bin/llama-server'),
    'models': {
        'fast': {'name': 'Qwen2.5-3B-Instruct', 'file': 'Qwen2.5-3B-Instruct-Q4_K_M.gguf', 'role': 'chat', 'flags': '-t 18 -tb 18 -fa -c 4096 -b 512 -ub 128 --parallel 1 --mlock --no-mmap'},
        'analyst': {'name': 'DeepSeek-R1-Distill-Qwen-14B', 'file': 'DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf', 'role': 'analyst', 'flags': '-t 18 -tb 18 -fa -c 4096 -b 512 -ub 128 --parallel 1 --mlock --no-mmap'},
        'coder': {'name': 'Qwen2.5-Coder-32B-Instruct', 'file': 'Qwen2.5-Coder-32B-Instruct-Q3_K_M.gguf', 'draft': 'qwen2.5-coder-0.5b-instruct-q8_0.gguf', 'role': 'coder', 'flags': '-t 18 -tb 18 -fa -c 4096 -b 512 -ub 128 --parallel 1 --mlock --no-mmap', 'draft_flags': '-ngld 99 --draft-max 8 --draft-min 4 --draft-p-min 0.75'},
    },
}

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'rb') as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if k == 'models' and isinstance(v, dict):
                for mk, mv in v.items():
                    cfg['models'].setdefault(mk, {}).update(mv)
            else:
                cfg[k] = v
    return cfg

def save_config(cfg):
    lines = [f'{k} = "{v}"' if isinstance(v, str) else f'{k} = {v}' for k, v in cfg.items() if k != 'models']
    for mk, mv in cfg['models'].items():
        lines.append(f'\n[models.{mk}]')
        for kk, vv in mv.items():
            lines.append(f'{kk} = "{vv}"' if isinstance(vv, str) else f'{kk} = {vv}')
    CONFIG_FILE.write_text('\n'.join(lines))

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.executescript('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL); CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT, content TEXT, reasoning TEXT, agent TEXT, created_at REAL);')
    conn.commit(); conn.close()
class Llama:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg['llama_base'].rstrip('/')
        p = urlparse(self.base)
        self.host, self.port, self.last_loaded = p.hostname or '127.0.0.1', str(p.port or 8081), None

    def health(self):
        try:
            r = requests.get(f'{self.base}/health', timeout=2)
            return r.status_code == 200 and r.json().get('status') == 'ok'
        except Exception:
            return False

    def _kill(self):
        subprocess.run(['pkill', '-f', 'llama-server'], check=False)
        time.sleep(1)

    def load(self, key):
        if self.last_loaded == key and self.health():
            return True
        m = self.cfg['models'][key]
        mp = Path(self.cfg['models_dir']).expanduser() / m['file']
        if not mp.exists():
            raise FileNotFoundError(f"Model not found: {mp} (run: codegent setup-models)")
        bin_p = Path(self.cfg['llama_server']).expanduser()
        if not bin_p.exists():
            raise FileNotFoundError(f"llama-server not found: {bin_p}")
        self._kill()
        cmd = [str(bin_p), '-m', str(mp), *m['flags'].split(), '--host', self.host, '--port', self.port]
        if m.get('draft'):
            dp = Path(self.cfg['models_dir']).expanduser() / m['draft']
            if dp.exists():
                cmd += ['-md', str(dp)] + m.get('draft_flags', '').split()
        with open(LOG_FILE, 'w') as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(240):
            if self.health():
                self.last_loaded = key
                return True
            time.sleep(0.5)
        raise RuntimeError("Model load timeout (120s)")

    def stream(self, messages, max_tokens=2048, temperature=0.7, keep_reasoning=True):
        payload = {'model': 'local-model', 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature, 'stream': True}
        with requests.post(f'{self.base}/v1/chat/completions', json=payload, stream=True, timeout=600) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line: continue
                line = line.decode('utf-8')
                if not line.startswith('data: ') or line[6:] == '[DONE]': continue
                try:
                    j = json.loads(line[6:])
                    d = (j.get('choices') or [{}]).get('delta', {})
                    rs = d.get('reasoning_content', '') if keep_reasoning else ''
                    c = d.get('content', '')
                    if rs or c: yield rs, c
                except Exception: pass
console = Console()

class CodegentCLI:
    def __init__(self):
        self.cfg = load_config()
        if not CONFIG_FILE.exists(): save_config(self.cfg)
        init_db()
        self.llama, self.mode, self.session_id = Llama(self.cfg), 'fast', None

    def handle_slash(self, cmd):
        parts = cmd[1:].split()
        name = parts.lower() if parts else ''
        args = parts[1:]
        if name in ('help', 'h', '?'):
            console.print("[cyan]/mode fast|coding[/cyan] · [cyan]/models[/cyan] · [cyan]/load <key>[/cyan] · [cyan]/sessions[/cyan] · [cyan]/new[/cyan] · [cyan]/config[/cyan] · [cyan]/exit[/cyan]")
        elif name == 'mode' and args and args in ('fast', 'coding'):
            self.mode = args; console.print(f"[green]✓[/green] mode → [bold]{self.mode}[/bold]")
        elif name == 'models':
            t = Table(show_header=True)
            for col in ('key', 'name', 'file', 'status'): t.add_column(col)
            for k, m in self.cfg['models'].items():
                p = Path(self.cfg['models_dir']).expanduser() / m['file']
                t.add_row(k, m['name'], m['file'], ('[green]✓[/green]' if p.exists() else '[red]✗[/red]') + (' [cyan]loaded[/cyan]' if self.llama.last_loaded == k else ''))
            console.print(t)
        elif name == 'load' and args and args in self.cfg['models']:
            with console.status(f"loading {args}..."): self.llama.load(args)
            console.print(f"[green]✓[/green] loaded {args}")
        elif name == 'sessions':
            c = sqlite3.connect(DB_FILE)
            for sid, title, ts in c.execute('SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC LIMIT 20').fetchall():
                console.print(f"  [dim]{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}[/dim]  {title}")
            c.close()
        elif name == 'new':
            self.session_id = None; console.print("[green]✓[/green] new session")
        elif name == 'config':
            console.print(f"[cyan]{CONFIG_FILE}[/cyan]")
        elif name in ('exit', 'quit', 'q'):
            raise EOFError()

    def extract_files(self, text):
        files, clean = [], text
        for m in reversed(list(re.finditer(r'@([^\s@]+)', text))):
            p = Path(m.group(1)).expanduser()
            if p.exists():
                files.append((str(p), p.read_text(errors='replace')))
                clean = clean[:m.start()] + clean[m.end():]
        return clean.strip(), files
    def stream_response(self, messages, label, model_key, max_tokens=2048, temp=0.7, keep_reasoning=True):
        console.print(f"\n[bold]{label}[/bold] [dim]· {self.cfg['models'][model_key]['name']}[/dim]")
        c_acc, r_acc = '', ''
        with Live(console=console, refresh_per_second=15) as live:
            for reason, content in self.llama.stream(messages, max_tokens=max_tokens, temperature=temp, keep_reasoning=keep_reasoning):
                if reason:
                    r_acc += reason
                    live.update(Panel(Text(r_acc[-800:], style='dim italic'), title='[dim]thinking[/dim]', border_style='dim purple'))
                if content:
                    c_acc += content
                    live.update(Panel(Markdown(c_acc), border_style='dim'))
        console.print()
        return c_acc

    def run_pipeline(self, prompt):
        try:
            if self.mode == 'fast':
                self.llama.load('fast')
                self.stream_response([{'role': 'system', 'content': 'Trả lời trực tiếp, ngắn gọn.'}, {'role': 'user', 'content': prompt}], 'ASSISTANT', 'fast', 1024)
            else:
                self.llama.load('analyst')
                sys1 = "Phân tích yêu cầu lập trình (KHÔNG viết code), xuất <coder_prompt>...</coder_prompt> chi tiết cho Coder."
                analysis = self.stream_response([{'role': 'system', 'content': sys1}, {'role': 'user', 'content': prompt}], 'ANALYST', 'analyst', 1200)
                m = re.search(r'<coder_prompt>(.*?)</coder_prompt>', analysis, re.DOTALL)
                cp = m.group(1).strip() if m else analysis
                self.llama.load('coder')
                self.stream_response([{'role': 'system', 'content': 'Viết CODE HOÀN CHỈNH, CHẠY ĐƯỢC.'}, {'role': 'user', 'content': cp}], 'CODER', 'coder', 2048, 0.2, False)
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def run(self):
        console.print(Panel("[bold]CODEGENT[/bold] · local multi-agent\n[dim]/help · Ctrl+D exit · @file attach[/dim]", border_style='dim'))
        kb = KeyBindings()
        @kb.add('c-j')
        def _(e): e.current_buffer.insert_text('\n')
        session = PromptSession(history=FileHistory(str(HISTORY_FILE)), auto_suggest=AutoSuggestFromHistory(), key_bindings=kb)
        while True:
            try:
                text = session.prompt(f'[{self.mode}] › ').strip()
            except (KeyboardInterrupt, EOFError):
                break
            if not text: continue
            if text.startswith('/'):
                try: self.handle_slash(text)
                except EOFError: break
                continue
            clean, files = self.extract_files(text)
            full = '\n\n'.join([f"--- FILE: {n} ---\n{c}\n--- END {n} ---" for n, c in files] + ([clean] if clean else [])) if files else text
            self.run_pipeline(full)

def cmd_setup_models():
    cfg = load_config()
    urls = {
        'Qwen2.5-3B-Instruct-Q4_K_M.gguf': 'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf',
        'DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf': 'https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf',
        'Qwen2.5-Coder-32B-Instruct-Q3_K_M.gguf': 'https://huggingface.co/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-32B-Instruct-Q3_K_M.gguf',
        'qwen2.5-coder-0.5b-instruct-q8_0.gguf': 'https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-0.5b-instruct-q8_0.gguf',
    }
    d = Path(cfg['models_dir']).expanduser(); d.mkdir(parents=True, exist_ok=True)
    for fname, url in urls.items():
        t = d / fname
        if not t.exists():
            subprocess.run(['wget', '-c', '--progress=bar:force', '-O', str(t), url], check=True)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv == 'setup-models': cmd_setup_models()
    else: CodegentCLI().run()
