"""Render the real CLI views using a disposable fixture and no model/LSP requests."""

import os
import tempfile
from pathlib import Path

import git
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.segment import Segment
from rich.terminal_theme import MONOKAI
from rich.text import Text
from rich.cells import cell_len

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.terminal_ui import session_status, show_context, toolbar


def main():
    destination = Path("benchmark/previews").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "example-repo"
        root.mkdir()
        os.chdir(root)
        coder = None
        repo = None
        try:
            repo = git.Repo.init(root)
            for filename in ("service.py", "models.py"):
                Path(filename).write_text(
                    "def process(value: int) -> int:\n" + "    value += 1\n" * 60
                    + "    return value\n", encoding="utf-8",
                )
            repo.index.add(["service.py", "models.py"])
            repo.index.commit("Preview fixture")
            io = InputOutput(pretty=False)
            coder = Coder.create(
                Model("gpt-4o"), "diff", io=io, fnames=[str(root / "service.py")],
                auto_commits=False, enable_lsp_graph=True, enable_agent_nav=True,
                enable_l2_paging=True,
            )
            coder.main_model.send_completion = lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("Preview cannot make model requests")
            )
            coder.agent_navigator.execute([("page_l1", ["models.py"])])
            coder.context_pager.demote_file(coder, str(root / "models.py"))
            coder.agent_navigator.execute([("page_l1", ["missing.py"])])
            console = Console(record=True, width=104, force_terminal=True)
            io.console, io.pretty, io.prompt_session = console, True, object()
            coder.show_announcements()
            console.print(Text("Preview fixture · no inference or language-server queries",
                               style="dim"))
            console.print(Text("\n> /status", style="bold cyan"))
            show_context(io, coder)
            console.print(Text("\n› Describe the next change…", style="bold cyan"))
            console.print(Text("".join(s for _, s in toolbar(session_status(coder), 104))),
                          style="dim")
            segments = list(console._record_buffer)
            console.save_svg(str(destination / "terminal-ui.svg"), title="Better Aider")
            svg = destination / "terminal-ui.svg"
            svg.write_text("\n".join(line.rstrip() for line in
                                     svg.read_text(encoding="utf-8").splitlines()) + "\n",
                           encoding="utf-8")
            # save_svg clears the record buffer by default; keep the original for HTML/PNG.
            console._record_buffer[:] = segments
            console.save_html(str(destination / "terminal-ui.html"), theme=MONOKAI, clear=False)
            font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/consola.ttf"
            if font_path.exists():
                font = ImageFont.truetype(str(font_path), 17)
                lines = list(Segment.split_lines(segments))
                cell_width, line_height = font.getlength("M"), 24
                image = Image.new(
                    "RGB", (int(104 * cell_width + 40), len(lines) * line_height + 40),
                    (18, 20, 24),
                )
                draw = ImageDraw.Draw(image)
                for row, line in enumerate(lines):
                    column = 0
                    for segment in line:
                        if segment.control:
                            continue
                        color = (222, 226, 233)
                        if segment.style and segment.style.color:
                            color = tuple(segment.style.color.get_truecolor(MONOKAI))
                        if segment.style and segment.style.dim:
                            color = tuple(int(c * .65 + bg * .35)
                                          for c, bg in zip(color, (18, 20, 24)))
                        draw.text((20 + column * cell_width, 20 + row * line_height), segment.text,
                                  fill=color, font=font)
                        column += cell_len(segment.text)
                image.save(destination / "terminal-ui.png")
            assert not coder.repo_map.semantic_graph.bridge.stats.get("queries", 0)
        finally:
            if coder is not None:
                coder.agent_navigator.close()
                coder.repo_map.close()
                coder.repo.repo.close()
            if repo is not None:
                repo.close()
            os.chdir(original)
    print(f"Preview written to {destination}")


if __name__ == "__main__":
    main()
