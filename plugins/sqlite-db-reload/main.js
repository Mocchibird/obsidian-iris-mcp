"use strict";

const { Plugin, Notice } = require("obsidian");

const SQLITE_DB_PLUGIN_ID = "sqlite-db";
const URI_ACTION = "sqlite-db-reload";

// [[path]] or [[path|display]]  — strict: whole cell, no surrounding text.
const WIKILINK_RE = /^\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]$/;

module.exports = class SqliteDbReloadPlugin extends Plugin {
    async onload() {
        console.log("[sqlite-db-reload] loaded");

        // ─── Command: Reload SQLite DB (assignable to a hotkey) ─────────
        this.addCommand({
            id: "reload-sqlite-db",
            name: "Reload SQLite DB",
            callback: () => this.reloadDb({ silent: false }),
        });

        // ─── obsidian:// URI handler ────────────────────────────────────
        //   open obsidian://sqlite-db-reload          (silent)
        //   open obsidian://sqlite-db-reload?notify=1 (with a toast)
        this.registerObsidianProtocolHandler(URI_ACTION, (params) => {
            const notify = params && params.notify === "1";
            this.reloadDb({ silent: !notify });
        });

        // ─── Linkify SQL table cells ────────────────────────────────────
        // The SQLite DB plugin renders cell content as plain text. We watch for
        // newly-inserted `.sql-results-td` nodes and replace cells whose entire
        // contents look like `[[wikilink]]` with real internal-link anchors.
        this._observer = new MutationObserver((mutations) => {
            for (const mut of mutations) {
                for (const node of mut.addedNodes) {
                    if (!node || node.nodeType !== 1) continue;
                    if (node.classList && node.classList.contains("sql-results-td")) {
                        this._linkifyCell(node);
                    }
                    if (typeof node.querySelectorAll === "function") {
                        node.querySelectorAll(".sql-results-td:not([data-iris-linkified])")
                            .forEach((td) => this._linkifyCell(td));
                    }
                }
            }
        });
        this._observer.observe(document.body, { childList: true, subtree: true });

        // Also sweep cells already in the DOM at load time (notes already open).
        document.querySelectorAll(".sql-results-td:not([data-iris-linkified])")
            .forEach((td) => this._linkifyCell(td));
    }

    onunload() {
        if (this._observer) {
            this._observer.disconnect();
            this._observer = null;
        }
        console.log("[sqlite-db-reload] unloaded");
    }

    _linkifyCell(td) {
        if (td.dataset.irisLinkified) return;
        const text = (td.textContent || "").trim();
        const m = text.match(WIKILINK_RE);
        if (!m) return;

        td.dataset.irisLinkified = "1";
        const target = m[1].trim();
        const display = (m[2] || target).trim();

        td.empty();
        const a = td.createEl("a", {
            cls: "internal-link",
            text: display,
        });
        a.setAttribute("href", target);
        a.setAttribute("data-href", target);
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener");

        a.addEventListener("click", (evt) => {
            evt.preventDefault();
            evt.stopPropagation();
            const newPane = evt.ctrlKey || evt.metaKey || evt.button === 1;
            this.app.workspace.openLinkText(target, "", newPane);
        });
        // Hover preview (page-preview core plugin)
        a.addEventListener("mouseover", (evt) => {
            this.app.workspace.trigger("hover-link", {
                event: evt,
                source: "sqlite-db-reload",
                hoverParent: a,
                targetEl: a,
                linktext: target,
            });
        });
    }

    async reloadDb({ silent }) {
        const target = this.app.plugins.plugins[SQLITE_DB_PLUGIN_ID];
        if (!target) {
            new Notice("SQLite DB plugin not loaded — nothing to reload.");
            return false;
        }
        if (typeof target.openDatabase !== "function") {
            new Notice("SQLite DB plugin missing openDatabase() — version mismatch?");
            return false;
        }

        try {
            await target.openDatabase(true);

            // Clear the linkify cache so re-rendered cells get re-processed.
            document.querySelectorAll(".sql-results-td[data-iris-linkified]")
                .forEach((td) => delete td.dataset.irisLinkified);

            // Re-render visible markdown views so SQL blocks re-execute.
            this.app.workspace.getLeavesOfType("markdown").forEach((leaf) => {
                const view = leaf.view;
                if (view && view.previewMode && typeof view.previewMode.rerender === "function") {
                    view.previewMode.rerender(true);
                }
            });

            if (!silent) new Notice("SQLite DB reloaded ✓");
            return true;
        } catch (err) {
            console.error("[sqlite-db-reload] reload failed:", err);
            new Notice("SQLite DB reload failed — see console.");
            return false;
        }
    }
};
