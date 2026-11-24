from flask import Flask, request, Response, send_from_directory, jsonify
import subprocess, requests, time, json, re, sqlite3, os, uuid

app = Flask(__name__, static_folder='.')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions.db')

MODEL_MODES = {"fast": "fast", "analyst": "draft", "coder": "coder"}
SWITCH_SCRIPT = "/home/entropy/switch_agent.sh"
SWITCH_FAST_SCRIPT = "/home/entropy/switch_fast.sh"
LLAMA_BASE = "http://127.0.0.1:8081"


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            display TEXT,
            reasoning TEXT,
            agent TEXT,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_msgs_session ON messages(session_id);
    ''')
    conn.commit()
    conn.close()

init_db()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


_last_loaded = None


def _health_ok():
    try:
        r = requests.get(f'{LLAMA_BASE}/health', timeout=2)
        return r.status_code == 200 and r.json().get('status') == 'ok'
    except:
        return False


def _wait_health(max_wait=120):
    start = time.time()
    while time.time() - start < max_wait:
        if _health_ok():
            return True
        time.sleep(0.7)
    return False


def ensure_model(key):
    global _last_loaded
    target = MODEL_MODES[key]
    if _last_loaded == target and _health_ok():
        return True, "cached"

    subprocess.run(["pkill", "-f", "llama-server"], check=False)
    time.sleep(1)
    for _ in range(10):
        try:
            requests.get(f'{LLAMA_BASE}/health', timeout=0.5)
        except requests.exceptions.ConnectionError:
            break
        except:
            pass
        time.sleep(0.5)

    try:
        if key == "fast":
            subprocess.run([SWITCH_FAST_SCRIPT], check=True)
        else:
            subprocess.run([SWITCH_SCRIPT, target], check=True)
    except subprocess.CalledProcessError as e:
        return False, f"switch failed: {e}"
    except FileNotFoundError:
        return False, f"script not found for {key}"

    if _wait_health():
        _last_loaded = target
        return True, "switched"
    return False, "health timeout"


def emit(type_, **kw):
    return f"data: {json.dumps({'type': type_, **kw})}\n\n"


def stream_llama(messages, max_tokens, temperature, keep_reasoning=True):
    payload = {"model": "local-model", "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "top_p": 0.9, "stream": True}
    try:
        with requests.post(f'{LLAMA_BASE}/v1/chat/completions', json=payload,
                           stream=True, timeout=600) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode('utf-8')
                if not line.startswith('data: '):
                    continue
                chunk = line[6:]
                if chunk == '[DONE]':
                    break
                try:
                    j = json.loads(chunk)
                    d = j.get('choices', [{}])[0].get('delta', {})
                    rs = d.get('reasoning_content', '') if keep_reasoning else ''
                    c = d.get('content', '')
                    if rs or c:
                        yield rs, c
                except:
                    pass
    except Exception as e:
        yield '', f'\n[ERR: {e}]\n'


def batch(gen, n=3, l=25):
    rb, cb, cn = '', '', 0
    for r, c in gen:
        if r:
            rb += r
            if len(rb) >= 60:
                yield ('r', rb); rb = ''
        if c:
            cb += c; cn += 1
            if cn >= n or len(cb) >= l:
                yield ('c', cb); cb = ''; cn = 0
    if rb: yield ('r', rb)
    if cb: yield ('c', cb)


def _save_assistant(sid, content, reasoning, agent):
    c = db()
    c.execute('INSERT INTO messages (session_id, role, content, display, reasoning, agent, created_at) VALUES (?,?,?,?,?,?,?)',
              (sid, 'assistant', content, content, reasoning, agent, time.time()))
    c.execute('UPDATE sessions SET updated_at=? WHERE id=?', (time.time(), sid))
    c.commit(); c.close()


# ==================== ROUTES ====================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/sessions')
def list_sessions():
    c = db()
    rows = c.execute('SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 100').fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/session/<sid>')
def get_session(sid):
    c = db()
    s = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    if not s:
        c.close()
        return jsonify({'error': 'not found'}), 404
    msgs = c.execute('SELECT role, content, display, reasoning, agent, created_at FROM messages WHERE session_id=? ORDER BY id', (sid,)).fetchall()
    c.close()
    return jsonify({'session': dict(s), 'messages': [dict(m) for m in msgs]})


@app.route('/api/session/<sid>', methods=['DELETE'])
def del_session(sid):
    c = db()
    c.execute('DELETE FROM messages WHERE session_id=?', (sid,))
    c.execute('DELETE FROM sessions WHERE id=?', (sid,))
    c.commit(); c.close()
    return jsonify({'ok': True})


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json or {}
    content = (data.get('content') or '').strip()
    display = (data.get('display') or content).strip()
    mode = data.get('mode', 'fast')
    sid = data.get('session_id')

    if not content:
        return Response(emit('error', message='Empty'), mimetype='text/event-stream')

    is_new = not sid
    if is_new:
        sid = str(uuid.uuid4())
        c = db()
        title = display[:60] if display else 'untitled'
        c.execute('INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)',
                  (sid, title, time.time(), time.time()))
        c.commit(); c.close()

    c = db()
    c.execute('INSERT INTO messages (session_id, role, content, display, reasoning, agent, created_at) VALUES (?,?,?,?,?,?,?)',
              (sid, 'user', content, display, None, None, time.time()))
    c.commit(); c.close()

    def generate():
        yield emit('session', id=sid, is_new=is_new, title=display[:60])
        try:
            if mode == 'fast':
                yield emit('status', agent='reasoner', message='Loading Qwen 3B...')
                ok, r = ensure_model('fast')
                if not ok:
                    yield emit('error', message=r); return
                yield emit('status', agent='reasoner', message='Generating...')
                yield emit('phase_start', agent='reasoner')

                sysp = "Bạn là AI assistant local, trả lời trực tiếp, ngắn gọn, đi thẳng vấn đề. Tiếng Việt nếu user hỏi tiếng Việt."
                content_acc, reasoning_acc = '', ''
                for kind, chunk in batch(stream_llama(
                    [{"role": "system", "content": sysp}, {"role": "user", "content": content}],
                    1024, 0.7)):
                    if kind == 'r':
                        reasoning_acc += chunk
                        yield emit('reasoning', content=chunk)
                    else:
                        content_acc += chunk
                        yield emit('content', content=chunk)

                yield emit('phase_end', agent='reasoner')
                _save_assistant(sid, content_acc, reasoning_acc, 'reasoner')
                yield emit('done')

            else:
                yield emit('status', agent='reasoner', message='Loading R1 analyst...')
                ok, r = ensure_model('analyst')
                if not ok:
                    yield emit('error', message=r); return
                yield emit('status', agent='reasoner', message='Analyzing & writing coder prompt...')
                yield emit('phase_start', agent='reasoner')

                analyst_sys = """Bạn là CHUYÊN GIA PHÂN TÍCH YÊU CẦU LẬP TRÌNH. Làm 3 bước:
BƯỚC 1: Suy nghĩ trong <think>...</think>.
BƯỚC 2: Phân tích NGẮN về ngôn ngữ, approach, edge cases.
BƯỚC 3: Viết <coder_prompt>...</coder_prompt> CHI TIẾT cho Qwen Coder (yêu cầu cụ thể, I/O format, edge cases, style, test cases).
QUY TẮC: KHÔNG viết code. <coder_prompt> là phần QUAN TRỌNG NHẤT."""

                full, r_acc = '', ''
                for kind, chunk in batch(stream_llama(
                    [{"role": "system", "content": analyst_sys}, {"role": "user", "content": content}],
                    1200, 0.7)):
                    if kind == 'r':
                        r_acc += chunk
                        yield emit('reasoning', content=chunk)
                    else:
                        full += chunk
                        yield emit('content', content=chunk)

                yield emit('phase_end', agent='reasoner')
                _save_assistant(sid, full, r_acc, 'reasoner')

                m = re.search(r'<coder_prompt>(.*?)</coder_prompt>', full, re.DOTALL)
                cp = m.group(1).strip() if m else full

                yield emit('status', agent='coder', message='Loading Qwen Coder 32B + draft...')
                ok, r = ensure_model('coder')
                if not ok:
                    yield emit('error', message=r); return
                yield emit('status', agent='coder', message='Coding...')
                yield emit('phase_start', agent='coder')

                coder_sys = "Bạn là LẬP TRÌNH VIÊN CHUYÊN NGHIỆP. Viết CODE HOÀN CHỈNH, CHẠY ĐƯỢC, có type hints, docstring, ví dụ test. Trả lời markdown."
                c_acc = ''
                for kind, chunk in batch(stream_llama(
                    [{"role": "system", "content": coder_sys}, {"role": "user", "content": f"PROMPT:\n\n{cp}"}],
                    2048, 0.2, keep_reasoning=False)):
                    if kind == 'c':
                        c_acc += chunk
                        yield emit('content', content=chunk)

                yield emit('phase_end', agent='coder')
                _save_assistant(sid, c_acc, '', 'coder')
                yield emit('done')

        except Exception as e:
            yield emit('error', message=f'{type(e).__name__}: {e}')

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    print("🚀 http://127.0.0.1:3000")
    app.run(host='127.0.0.1', port=3000, threaded=True, debug=False)
