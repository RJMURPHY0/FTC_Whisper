"""One visual frame per layout change.

Tk paints asynchronously: a pack/grid change is turned into queued Expose
events and drawn on some later mainloop spin. So a multi-step layout change —
swap a page, pack a status row, show/hide a card — is not one repaint, it is
several, and the user sees the intermediate states as tearing, duplicated rows
or a strip of the previous page ("the glitch"). Chasing it with RedrawWindow
afterwards is a race, and every version that tried that lost it sometimes.

`atomic()` makes the change indivisible instead:

    freeze painting → mutate → settle the layout → present once

Rules learned the hard way, each one a shipped bug:

* The freeze is SKIPPED while the window is unmapped. Toggling WM_SETREDRAW
  clears WS_VISIBLE; doing that across an in-progress map leaves the on-screen
  bits and Tk's idea of what it has drawn out of sync (the intermittent ghost
  on the returning-user path, where a fast session restore promotes the
  dashboard microseconds after deiconify()).
* The window RESIZE never happens inside the freeze — calling geometry() while
  painting is frozen leaves the just-packed frame UNMAPPED, which is the stale
  page plus a white strip where the window grew. It is applied after painting
  is back on, via the `geometry` callback.
* A geometry change needs an ERASING repaint. RDW_UPDATENOW only validates
  Tk's update region, so a move or grow otherwise blits the old pixels into
  their new position and leaves them there, and a newly exposed strip has no
  widget above it to paint over it.
* A same-size change must NOT erase: erasing flashes the whole background for a
  frame, which is its own glitch — and same-size swaps (tab clicks, opening a
  panel) are the common case.
* RedrawWindow(NULL) means the DESKTOP. With no HWND, do nothing instead.
"""

import ctypes

_WM_SETREDRAW = 0x000B
# RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW
_RDW_REPAINT = 0x181
# ... | RDW_ERASE | RDW_FRAME
_RDW_ERASE = 0x585


def top_hwnd(widget) -> int:
    """Top-level HWND of `widget`'s window. GetAncestor(GA_ROOT) — never
    resolve by title: during an update handoff two processes both own an
    'FTC Whisper' window, and freezing the other one's painting would wedge
    it."""
    try:
        u32 = ctypes.windll.user32
        wid = widget.winfo_id()
        return u32.GetAncestor(wid, 2) or u32.GetParent(wid) or wid
    except Exception:
        return 0


def repaint(hwnd: int, erase: bool = False) -> None:
    """Synchronous full repaint of a window tree. See the module note on when
    erase is required and when it is a glitch of its own."""
    if not hwnd:
        return
    try:
        ctypes.windll.user32.RedrawWindow(
            hwnd, None, None, _RDW_ERASE if erase else _RDW_REPAINT)
    except Exception:
        pass


_frozen: set = set()


def atomic(root, fn, hwnd: int = 0, geometry=None, settle: bool = True,
           erase: bool = True) -> None:
    """Run `fn()` as one visual frame on `root`'s window.

    hwnd     — top-level to freeze; resolved from `root` when omitted.
    geometry — optional callable run AFTER painting is re-enabled, returning a
               geometry string to apply (or None). Never resize inside `fn`.
    settle   — call update_idletasks() inside the freeze. Pass False when the
               caller drains the layout itself (it may need to hold state
               across that drain).
    erase    — how the frame is presented. True (page swaps) repaints the
               background first, which is invisible when the whole page is
               changing and covers anything the old page left behind. False
               (a row appearing inside a page, a canvas re-render) repaints
               over: an unmapped widget's parent redraws the space it vacated,
               so the erase would only add a whole-window background flash —
               a glitch of its own, on a change the user expects to be local.

    Nesting is safe and does nothing: WM_SETREDRAW is a flag, not a counter, so
    an inner call's unfreeze would re-enable painting halfway through the outer
    change and present it half-done. The outer frame owns the present.
    """
    if not hwnd:
        hwnd = top_hwnd(root)
    if hwnd and hwnd in _frozen:
        fn()
        return
    try:
        mapped = bool(root.winfo_ismapped())
    except Exception:
        mapped = True
    froze = False
    try:
        if hwnd and mapped:
            ctypes.windll.user32.SendMessageW(hwnd, _WM_SETREDRAW, 0, 0)
            froze = True
    except Exception:
        froze = False
    if hwnd:
        _frozen.add(hwnd)
    try:
        fn()
        if settle:
            try:
                root.update_idletasks()
            except Exception:
                pass
    finally:
        _frozen.discard(hwnd)
        if froze:
            try:
                ctypes.windll.user32.SendMessageW(hwnd, _WM_SETREDRAW, 1, 0)
                ctypes.windll.user32.RedrawWindow(
                    hwnd, None, None, _RDW_ERASE if erase else _RDW_REPAINT)
            except Exception:
                pass
        moved = False
        if geometry is not None:
            try:
                geo = geometry()
            except Exception:
                geo = None
            if geo:
                moved = True
                try:
                    root.geometry(geo)
                    root.update_idletasks()
                except Exception:
                    pass
        # Heal: update_idletasks drains idle callbacks, NOT the Expose events
        # the swap (and any geometry change) just queued. The heal erases only
        # when the window MOVED or grew — that is the one case where pixels
        # land outside every widget's area and repaint-over cannot reach them.
        repaint(hwnd, erase=moved)
        try:
            root.after(0, lambda e=moved: repaint(hwnd, erase=e))
        except Exception:
            pass
