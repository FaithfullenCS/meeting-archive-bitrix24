import ast
from pathlib import Path
from types import SimpleNamespace


def test_real_open_menu_handler_defers_activation_until_menu_returns():
    source = Path(__file__).resolve().parents[1] / 'meeting_archive/launcher.py'
    tree = ast.parse(source.read_text('utf-8'))
    handler = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'open_ui')
    calls, posted = [], []
    scope = {'url': 'http://localhost:8765/?launch=synthetic', 'open_browser': lambda url: calls.append(url)}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), scope)
    icon = SimpleNamespace(open_archive=lambda: posted.append(lambda: scope['open_browser'](scope['url'])))
    scope['open_ui'](icon, None)  # Still inside the context menu callback.
    assert not calls, 'Window activation happened before the tray menu callback returned'
    assert len(posted) == 1
    posted.pop()()
    assert calls == [scope['url']]


def test_bound_open_action_posts_native_messages_and_never_waits_inside_menu():
    from meeting_archive.tray import bind_open_action, WM_OPEN_ARCHIVE
    calls, messages = [], []
    icon=SimpleNamespace(_hwnd=42,_menu_hwnd=43,_message_handlers={})
    def post(*args):
        messages.append(args)
        return True
    bind_open_action(icon,lambda: calls.append('open'),post=post)
    icon.open_archive()
    icon.open_archive()  # Repeated clicks before the next message coalesce.
    assert not calls
    assert messages == [(43,0,0,0),(42,WM_OPEN_ARCHIVE,0,0)]
    icon._message_handlers[WM_OPEN_ARCHIVE](0,0)
    assert calls==['open']
    icon.open_archive()
    icon._message_handlers[WM_OPEN_ARCHIVE](0,0)
    assert calls==['open','open']
