import { useEffect, useMemo, useRef, useState } from "react";

export interface Option {
  value: string;
  label: string;
  hint?: string;
}

interface Props {
  options: Option[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  loading?: boolean;
}

export default function SearchableSelect({ options, value, onChange, placeholder, loading }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
    );
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setHighlight(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const selected = options.find((o) => o.value === value);

  return (
    <div className="searchable-select" ref={rootRef}>
      <button
        type="button"
        className="searchable-select-trigger"
        onClick={() => setOpen((o) => !o)}
      >
        <span className={selected ? "" : "muted"}>{selected?.label ?? placeholder ?? "Select…"}</span>
        <span className="chevron">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="searchable-select-menu">
          <input
            ref={inputRef}
            className="searchable-select-search"
            value={query}
            placeholder="Search…"
            onChange={(e) => {
              setQuery(e.target.value);
              setHighlight(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setHighlight((h) => Math.min(h + 1, filtered.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setHighlight((h) => Math.max(h - 1, 0));
              } else if (e.key === "Enter" && filtered[highlight]) {
                e.preventDefault();
                onChange(filtered[highlight].value);
                setOpen(false);
              } else if (e.key === "Escape") {
                setOpen(false);
              }
            }}
          />
          <div className="searchable-select-list">
            {loading && <div className="muted" style={{ padding: "0.5rem" }}>Loading…</div>}
            {!loading && filtered.length === 0 && (
              <div className="muted" style={{ padding: "0.5rem" }}>No matches</div>
            )}
            {filtered.map((o, i) => (
              <button
                type="button"
                key={o.value}
                className={`searchable-select-item${i === highlight ? " active" : ""}${o.value === value ? " selected" : ""}`}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
              >
                <span className="item-label">{o.label}</span>
                {o.hint && <span className="item-hint muted">{o.hint}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
