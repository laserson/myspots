from __future__ import annotations

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Key
from textual.widgets import (
    Checkbox,
    Footer,
    Input,
    Label,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from myspots import (
    NotionMySpotsStore,
    get_detailed_place_data,
    get_google_maps_client,
    query_places_api,
)
from myspots.cache import MySpotsCache


class FlagCheckbox(Checkbox):
    """Checkbox that shows ✓ when checked and blank when unchecked."""

    BUTTON_INNER = " "

    def watch_value(self) -> None:
        self.BUTTON_INNER = "✓" if self.value else " "
        super().watch_value()


def _flag_id(name: str) -> str:
    return f"flag-{name.lower().replace(' ', '-')}"


CSS = """
$border-color-nofocus: $panel;
$border-color-focus: $primary;

Screen {
    background: $surface;
}

#left-panel {
    width: 1fr;
    border-right: inner $border-color-nofocus;
    padding: 1 2;
}

#right-panel {
    width: 1fr;
    padding: 1 2;
}

#place-header {
    height: auto;
    margin-bottom: 1;
}

.field-label {
    color: $text-muted;
    margin-top: 1;
}

.help-text {
    color: $text-muted;
    height: auto;
    margin-top: 1;
}

Input {
    border: none;
    background: $boost;
    height: 1;
    padding: 0 1;
}
Input:focus {
    border: none;
    background: $panel;
}

OptionList {
    border: none;
    background: transparent;
    height: auto;
    max-height: 12;
    padding: 0;
    scrollbar-size: 0 0;
}

#results-list {
    height: 1fr;
    max-height: 100%;
}

#category-suggestions, #tag-suggestions {
    max-height: 5;
    color: $text-muted;
}

#selected-tags {
    height: auto;
}

#flags-container {
    height: auto;
    layout: horizontal;
    margin-top: 1;
}

Checkbox {
    background: transparent;
    border: none;
    height: 1;
    padding: 0;
    margin: 0 3 0 0;
}

TextArea {
    border: none;
    background: $boost;
    height: 4;
    padding: 0 1;
}
TextArea:focus {
    border: none;
    background: $panel;
}

Footer {
    background: $boost;
}
FooterKey .footer-key--key {
    text-style: bold;
    background: $panel;
}
"""


class MySpotsApp(App):
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit"),
        Binding("ctrl+r", "reset", "Reset"),
        Binding("shift+tab", "focus_results", "Results", show=False),
        Binding("escape", "quit", "Quit"),
    ]

    def __init__(self, config: dict, refresh_cache: bool = False):
        super().__init__()
        self.config = config
        self.refresh_cache = refresh_cache
        self.cache = MySpotsCache()
        self.cache.load()
        self.store: NotionMySpotsStore | None = None
        self.google_client = None
        self.search_results: list[dict] = []
        self.selected_indices: set[int] = set()
        self.selected_categories: list[dict] = []  # [{id, name}, ...]
        self.selected_tags: list[str] = []
        self._submitting: bool = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Label("[bold]Search[/]")
                yield Input(placeholder="search for a place…", id="search-input")
                yield Label("Location", classes="field-label")
                yield Input(
                    placeholder="e.g. New York, NY",
                    id="location-input",
                    value=self.cache.last_location,
                )
                yield Label("Results", classes="field-label")
                yield OptionList(id="results-list")
                yield Static(
                    "[dim]Enter[/] toggle  [dim]Tab[/] annotate  "
                    "[bold green]◆[/] selected  [bold yellow]★[/] in Notion",
                    classes="help-text",
                )
            with VerticalScroll(id="right-panel"):
                yield Static("", id="place-header")
                yield Label("Category", classes="field-label")
                yield Static("", id="selected-categories")
                yield Input(placeholder="type to filter…", id="category-input")
                yield OptionList(id="category-suggestions")
                yield Label("Tags", classes="field-label")
                yield Static("", id="selected-tags")
                yield Input(placeholder="type to add…", id="tag-input")
                yield OptionList(id="tag-suggestions")
                yield Label("Flags", classes="field-label")
                yield Horizontal(id="flags-container")
                yield Label("Notes", classes="field-label")
                yield TextArea(id="notes-area")
                yield Static(
                    "[dim]Enter[/] select category/tag  [dim]Space[/] toggle flags",
                    classes="help-text",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#category-suggestions", OptionList).can_focus = False
        self.query_one("#tag-suggestions", OptionList).can_focus = False
        self.query_one("#right-panel", VerticalScroll).can_focus = False
        self.query_one("#search-input", Input).focus()
        self._init_cache()

    @work(thread=True)
    def _init_cache(self) -> None:
        self.google_client = get_google_maps_client(self.config)
        self.store = NotionMySpotsStore(self.config)
        if not self.cache._is_fresh() or self.refresh_cache:
            self.cache.refresh(self.store)
        elif not self.cache.known_place_ids:
            self.cache.known_place_ids = self.store.fetch_known_place_ids()
            self.cache.save()
        self.app.call_from_thread(self._populate_flags)

    def _populate_flags(self) -> None:
        container = self.query_one("#flags-container", Horizontal)
        for flag_name in self.cache.flags:
            cb = FlagCheckbox(flag_name, id=_flag_id(flag_name))
            container.mount(cb)

    _INPUT_TO_SUGGESTIONS = {
        "category-input": "category-suggestions",
        "tag-input": "tag-suggestions",
    }

    def _navigate_suggestions(self, suggestion_list_id: str, direction: int) -> bool:
        sl = self.query_one(f"#{suggestion_list_id}", OptionList)
        if sl.option_count == 0:
            return False
        current = sl.highlighted
        if current is None:
            sl.highlighted = 0
        else:
            new = current + direction
            if 0 <= new < sl.option_count:
                sl.highlighted = new
        return True

    @on(Key)
    def on_key(self, event: Key) -> None:
        focused = self.focused
        if not isinstance(focused, Input) or event.key not in ("up", "down"):
            return
        suggestions_id = self._INPUT_TO_SUGGESTIONS.get(focused.id)
        if suggestions_id and self._navigate_suggestions(suggestions_id, -1 if event.key == "up" else 1):
            event.prevent_default()

    def _try_get_flag(self, name: str) -> bool:
        try:
            return self.query_one(f"#{_flag_id(name)}", FlagCheckbox).value
        except NoMatches:
            return False

    # --- Search ---

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("search-input", "location-input"):
            self._do_search()
        elif event.input.id == "category-input":
            self._accept_category_suggestion()
        elif event.input.id == "tag-input":
            self._accept_tag_suggestion()

    @work(thread=True)
    def _do_search(self) -> None:
        query = self.app.call_from_thread(lambda: self.query_one("#search-input", Input).value)
        location = self.app.call_from_thread(lambda: self.query_one("#location-input", Input).value)
        if not query.strip():
            return

        def show_loading():
            rl = self.query_one("#results-list", OptionList)
            rl.clear_options()
            rl.add_option(Option("searching…", disabled=True))

        self.app.call_from_thread(show_loading)

        results = query_places_api(
            self.google_client, query=query, location=location or None
        )
        self.search_results = results
        self.selected_indices.clear()

        def update_results():
            rl = self.query_one("#results-list", OptionList)
            rl.clear_options()
            if not results:
                rl.add_option(Option("no results", disabled=True))
                return
            for i, r in enumerate(results):
                rl.add_option(Option(self._format_result(i, r)))
            if len(results) == 1:
                self._toggle_result(0)
                self.query_one("#category-input", Input).focus()
            else:
                rl.highlighted = 0
                rl.focus()

        self.app.call_from_thread(update_results)

    def _format_result(self, index: int, result: dict) -> str:
        name = result.get("name", "Unknown")
        addr = result.get("formatted_address", "")
        pid = result.get("place_id", "")
        selected = "[bold green]◆[/] " if index in self.selected_indices else "  "
        exists = "[bold yellow]★[/] " if pid in self.cache.known_place_ids else "  "
        return f"{selected}{exists}[bold]{name}[/]  [dim italic]{addr}[/]"

    def _refresh_results_display(self) -> None:
        rl = self.query_one("#results-list", OptionList)
        highlighted = rl.highlighted
        rl.clear_options()
        for i, r in enumerate(self.search_results):
            rl.add_option(Option(self._format_result(i, r)))
        if highlighted is not None and highlighted < rl.option_count:
            rl.highlighted = highlighted

    def _toggle_result(self, index: int) -> None:
        if index in self.selected_indices:
            self.selected_indices.discard(index)
        else:
            self.selected_indices.add(index)
        self._refresh_results_display()
        self._update_place_header()

    def _update_place_header(self) -> None:
        if not self.selected_indices:
            self.query_one("#place-header", Static).update("")
            return
        lines = []
        for i in sorted(self.selected_indices):
            r = self.search_results[i]
            name = r.get("name", "Unknown")
            addr = r.get("formatted_address", "")
            pid = r.get("place_id", "")
            exists = " [bold yellow]★ in Notion[/]" if pid in self.cache.known_place_ids else ""
            lines.append(f"[bold green]◆[/] [bold]{name}[/]{exists}\n  [dim italic]{addr}[/]")
        self.query_one("#place-header", Static).update("\n".join(lines))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "results-list":
            self._toggle_result(event.option_index)
        elif event.option_list.id == "category-suggestions":
            self._pick_category(event.option_index)
        elif event.option_list.id == "tag-suggestions":
            self._pick_tag(event.option_index)

    # --- Category ---

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "category-input":
            self._filter_categories(event.value)
        elif event.input.id == "tag-input":
            self._filter_tags(event.value)

    def _filter_categories(self, text: str) -> None:
        sl = self.query_one("#category-suggestions", OptionList)
        sl.clear_options()
        if not text.strip():
            return
        text_lower = text.lower()
        for c in self.cache.categories:
            if text_lower in c["name"].lower():
                sl.add_option(Option(c["name"]))

    def _accept_category_suggestion(self) -> None:
        sl = self.query_one("#category-suggestions", OptionList)
        if sl.option_count > 0:
            idx = sl.highlighted if sl.highlighted is not None else 0
            self._pick_category(idx)

    def _pick_category(self, index: int) -> None:
        cat_input = self.query_one("#category-input", Input)
        text_lower = cat_input.value.lower()
        matches = [c for c in self.cache.categories if text_lower in c["name"].lower()]
        if index < len(matches):
            chosen = matches[index]
            if not any(c["id"] == chosen["id"] for c in self.selected_categories):
                self.selected_categories.append(chosen)
                self._render_selected_categories()
            cat_input.value = ""
            self.query_one("#category-suggestions", OptionList).clear_options()

    def _render_selected_categories(self) -> None:
        cats = " ".join(f"[bold magenta]{c['name']}[/]" for c in self.selected_categories)
        self.query_one("#selected-categories", Static).update(cats)

    # --- Tags ---

    def _filter_tags(self, text: str) -> None:
        sl = self.query_one("#tag-suggestions", OptionList)
        sl.clear_options()
        if not text.strip():
            return
        text_lower = text.lower()
        for t in self.cache.tags:
            if text_lower in t.lower() and t not in self.selected_tags:
                sl.add_option(Option(t))

    def _accept_tag_suggestion(self) -> None:
        sl = self.query_one("#tag-suggestions", OptionList)
        if sl.option_count > 0:
            idx = sl.highlighted if sl.highlighted is not None else 0
            self._pick_tag(idx)
        else:
            tag_input = self.query_one("#tag-input", Input)
            tag = tag_input.value.strip()
            if tag and tag not in self.selected_tags:
                is_new = tag not in self.cache.tags
                self.selected_tags.append(tag)
                self._render_selected_tags()
                tag_input.value = ""
                if is_new:
                    self.cache.tags.append(tag)
                    self.cache.save()
                    self.notify(f"New tag: {tag}")

    def _pick_tag(self, index: int) -> None:
        tag_input = self.query_one("#tag-input", Input)
        text_lower = tag_input.value.lower()
        matches = [
            t for t in self.cache.tags
            if text_lower in t.lower() and t not in self.selected_tags
        ]
        if index < len(matches):
            self.selected_tags.append(matches[index])
            self._render_selected_tags()
            tag_input.value = ""
            self.query_one("#tag-suggestions", OptionList).clear_options()

    def _render_selected_tags(self) -> None:
        tags = " ".join(f"[bold cyan]{t}[/]" for t in self.selected_tags)
        self.query_one("#selected-tags", Static).update(tags)

    # --- Submit ---

    @work(thread=True)
    def action_submit(self) -> None:
        if self._submitting:
            return
        if not self.selected_indices:
            self.app.call_from_thread(lambda: self.notify("No places selected", severity="error"))
            return
        if not self.store:
            self.app.call_from_thread(lambda: self.notify("Still loading…", severity="warning"))
            return

        self._submitting = True
        count = len(self.selected_indices)
        self.app.call_from_thread(lambda: self.notify(f"Submitting {count} place(s)…"))

        try:
            def _read_flags():
                return [
                    name for name in self.cache.flags
                    if self._try_get_flag(name)
                ]

            selected_flags = self.app.call_from_thread(_read_flags)

            notes = self.app.call_from_thread(lambda: self.query_one("#notes-area", TextArea).text)
            notes = notes.strip() or None

            added = []
            skipped = []
            for i in sorted(self.selected_indices):
                result = self.search_results[i]
                pid = result.get("place_id", "")

                if pid in self.cache.known_place_ids:
                    skipped.append(result.get("name", "Unknown"))
                    continue

                place = get_detailed_place_data(self.google_client, pid)
                if not place:
                    skipped.append(result.get("name", "Unknown"))
                    continue

                cat_ids = [c["id"] for c in self.selected_categories] or None
                self.store.insert_spot(
                    place,
                    notes=notes,
                    category_ids=cat_ids,
                    tags=self.selected_tags or None,
                    flags=selected_flags or None,
                )
                self.cache.add_known_place_id(place.google_place_id)
                added.append(place.name)

            location = self.app.call_from_thread(lambda: self.query_one("#location-input", Input).value)
            if location.strip():
                self.cache.add_location(location.strip())

            def finish():
                parts = []
                if added:
                    parts.append(f"Added: {', '.join(added)}")
                if skipped:
                    parts.append(f"Skipped (exists): {', '.join(skipped)}")
                self.notify(" | ".join(parts) if parts else "Nothing to add")
                self._clear_form()

            self.app.call_from_thread(finish)
        finally:
            self._submitting = False

    def action_focus_results(self) -> None:
        self.query_one("#results-list", OptionList).focus()

    def action_reset(self) -> None:
        self._clear_form()

    def _clear_form(self) -> None:
        self.query_one("#search-input", Input).value = ""
        self.query_one("#results-list", OptionList).clear_options()
        self.query_one("#place-header", Static).update("")
        self.query_one("#category-input", Input).value = ""
        self.query_one("#category-suggestions", OptionList).clear_options()
        self.query_one("#tag-input", Input).value = ""
        self.query_one("#tag-suggestions", OptionList).clear_options()
        self.selected_tags.clear()
        self._render_selected_tags()
        self.query_one("#notes-area", TextArea).clear()

        for flag_name in self.cache.flags:
            try:
                self.query_one(f"#{_flag_id(flag_name)}", FlagCheckbox).value = False
            except NoMatches:
                pass

        self.selected_indices.clear()
        self.selected_categories.clear()
        self._render_selected_categories()
        self.search_results = []
        self.query_one("#search-input", Input).focus()
