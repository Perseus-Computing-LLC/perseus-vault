//! #918: read-only TUI inspector (ratatui).
//!
//! `perseus-vault inspect --db <path> [--key-file <path>]` opens a vault
//! database STRICTLY read-only and renders four surfaces over the
//! `crate::inspect` data layer:
//!
//! * **Overview** — entity totals by state (active / archived / quarantined /
//!   superseded), claim cards (#852), decay-score histogram, top categories,
//!   recall-arm telemetry totals (served_events / recall_arm_audits /
//!   displacement_events);
//! * **Entities** — browsable list (state/category/text filters) with a
//!   detail pane: decrypted body, metadata, links, bi-temporal history;
//! * **Telemetry** — recent served events, arm audits, displacement events.
//!
//! There is deliberately NO write path here: repairs go through the governed
//! MCP tools (forget / supersede / quarantine / promote). This module is
//! compiled only when the `tui` feature is on (default).

use crate::inspect::{
    ArmAudit, DisplacementEvent, EntityFilter, HistoryRow, InspectEntity, Inspector, LinkRow,
    Overview, ServedEvent,
};
use crossterm::event::{self, Event, KeyCode};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, Borders, Cell, List, ListItem, ListState, Paragraph, Row, Table, Tabs,
};
use ratatui::{Frame, Terminal};

const STATE_FILTERS: [&str; 6] = [
    "all",
    "active",
    "archived",
    "quarantined",
    "superseded",
    "claims",
];

/// Civil-from-days: epoch ms -> "YYYY-MM-DD HH:MM:SS" (UTC). No chrono dep.
fn fmt_ts(ms: i64) -> String {
    if ms <= 0 {
        return "—".to_string();
    }
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    let (h, m, s) = (
        rem / 3_600_000,
        (rem % 3_600_000) / 60_000,
        (rem % 60_000) / 1000,
    );
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mth <= 2 { y + 1 } else { y };
    format!("{y:04}-{mth:02}-{d:02} {h:02}:{m:02}:{s:02}")
}

fn state_badges(e: &InspectEntity) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    if e.archived {
        spans.push(Span::styled(
            " ARCHIVED ",
            Style::default().fg(Color::DarkGray),
        ));
    }
    if e.quarantined {
        spans.push(Span::styled(
            " QUARANTINED ",
            Style::default()
                .fg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        ));
    }
    if e.superseded {
        spans.push(Span::styled(
            " SUPERSEDED ",
            Style::default().fg(Color::Blue),
        ));
    }
    if e.claim_card {
        spans.push(Span::styled(" CLAIM ", Style::default().fg(Color::Magenta)));
    }
    spans
}

fn fmt_decay(d: f64) -> String {
    format!("{d:.2}")
}

pub struct App {
    inspector: Inspector,
    tab: usize,
    filter_idx: usize,
    entities: Vec<InspectEntity>,
    selected: usize,
    detail: Option<(InspectEntity, Vec<HistoryRow>, Vec<LinkRow>)>,
    detail_scroll: u16,
    status: String,
    overview: Option<Overview>,
    served: Vec<ServedEvent>,
    audits: Vec<ArmAudit>,
    displacements: Vec<DisplacementEvent>,
}

impl App {
    fn new(inspector: Inspector) -> Self {
        Self {
            inspector,
            tab: 0,
            filter_idx: 0,
            entities: Vec::new(),
            selected: 0,
            detail: None,
            detail_scroll: 0,
            status: String::new(),
            overview: None,
            served: Vec::new(),
            audits: Vec::new(),
            displacements: Vec::new(),
        }
    }

    fn refresh(&mut self) {
        match self.inspector.overview() {
            Ok(ov) => self.overview = Some(ov),
            Err(e) => self.status = format!("overview: {e}"),
        }
        let filter = EntityFilter {
            state: Some(STATE_FILTERS[self.filter_idx].to_string()),
            ..Default::default()
        };
        match self.inspector.entities(&filter, 500) {
            Ok(list) => {
                self.entities = list;
                if self.selected >= self.entities.len() {
                    self.selected = 0;
                }
            }
            Err(e) => self.status = format!("entities: {e}"),
        }
        match self.inspector.recent_served(200) {
            Ok(v) => self.served = v,
            Err(e) => self.status = format!("served: {e}"),
        }
        match self.inspector.recent_arm_audits(200) {
            Ok(v) => self.audits = v,
            Err(e) => self.status = format!("arm audits: {e}"),
        }
        match self.inspector.recent_displacements(200) {
            Ok(v) => self.displacements = v,
            Err(e) => self.status = format!("displacements: {e}"),
        }
        if self.status.is_empty() {
            self.status = "read-only · q quit · tab sections · f filter · enter detail · r refresh"
                .to_string();
        }
    }

    fn open_detail(&mut self) {
        if let Some(e) = self.entities.get(self.selected) {
            match self.inspector.entity_detail(&e.id) {
                Ok(Some(detail)) => {
                    self.detail = Some(detail);
                    self.detail_scroll = 0;
                }
                Ok(None) => self.status = "entity vanished between refresh and detail".to_string(),
                Err(err) => self.status = format!("detail: {err}"),
            }
        }
    }

    fn handle_key(&mut self, key: KeyCode) {
        match self.tab {
            1 if self.detail.is_some() => match key {
                KeyCode::Esc | KeyCode::Enter | KeyCode::Left => {
                    self.detail = None;
                    self.status.clear();
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    self.detail_scroll = self.detail_scroll.saturating_sub(1)
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    self.detail_scroll = self.detail_scroll.saturating_add(1)
                }
                KeyCode::PageUp => self.detail_scroll = self.detail_scroll.saturating_sub(10),
                KeyCode::PageDown => self.detail_scroll = self.detail_scroll.saturating_add(10),
                _ => {}
            },
            _ => match key {
                KeyCode::Char('q') | KeyCode::Esc => {
                    // Esc with no detail open quits.
                    if self.detail.is_none() {
                        self.status = "__quit__".to_string();
                    }
                }
                KeyCode::Char('f') if self.tab == 1 => {
                    self.filter_idx = (self.filter_idx + 1) % STATE_FILTERS.len();
                    self.selected = 0;
                    self.refresh();
                }
                KeyCode::Char('r') => self.refresh(),
                KeyCode::Tab => {
                    self.tab = (self.tab + 1) % 3;
                    self.detail = None;
                }
                KeyCode::BackTab => {
                    self.tab = (self.tab + 2) % 3;
                    self.detail = None;
                }
                KeyCode::Char('1') => self.tab = 0,
                KeyCode::Char('2') => self.tab = 1,
                KeyCode::Char('3') => self.tab = 2,
                KeyCode::Up | KeyCode::Char('k') => {
                    self.selected = self.selected.saturating_sub(1);
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    if self.selected + 1 < self.entities.len() {
                        self.selected += 1;
                    }
                }
                KeyCode::PageUp => self.selected = self.selected.saturating_sub(10),
                KeyCode::PageDown => {
                    self.selected = (self.selected + 10).min(self.entities.len().saturating_sub(1));
                }
                KeyCode::Enter if self.tab == 1 => self.open_detail(),
                _ => {}
            },
        }
    }
}

fn draw_header(f: &mut Frame, app: &App, area: Rect) {
    let title = Line::from(vec![
        Span::styled(
            " Perseus Vault — read-only inspector ",
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  "),
        Span::styled(
            format!(
                "filter: {} · {} entities shown",
                STATE_FILTERS[app.filter_idx],
                app.entities.len()
            ),
            Style::default().fg(Color::Gray),
        ),
    ]);
    f.render_widget(
        Paragraph::new(title).block(Block::default().borders(Borders::NONE)),
        area,
    );
}

fn draw_tabs(f: &mut Frame, app: &App, area: Rect) {
    let titles = vec![" 1 Overview ", " 2 Entities ", " 3 Telemetry "];
    let tabs = Tabs::new(titles)
        .select(app.tab)
        .highlight_style(
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        )
        .block(Block::default().borders(Borders::ALL).title(" Sections "));
    f.render_widget(tabs, area);
}

fn draw_overview(f: &mut Frame, app: &App, area: Rect) {
    let Some(ov) = &app.overview else {
        f.render_widget(Paragraph::new("no data"), area);
        return;
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(6),
            Constraint::Length(5),
            Constraint::Min(3),
        ])
        .split(area);

    let state_rows = vec![
        Row::new(vec![
            Cell::from("total"),
            Cell::from(ov.total_entities.to_string()),
            Cell::from(""),
            Cell::from(""),
        ]),
        Row::new(vec![
            Cell::from("active"),
            Cell::from(ov.active.to_string()),
            Cell::from("archived"),
            Cell::from(ov.archived.to_string()),
        ]),
        Row::new(vec![
            Cell::from("quarantined"),
            Cell::from(ov.quarantined.to_string()),
            Cell::from("superseded"),
            Cell::from(ov.superseded.to_string()),
        ]),
        Row::new(vec![
            Cell::from("claim cards"),
            Cell::from(ov.claim_cards.to_string()),
            Cell::from("categories"),
            Cell::from(ov.categories.len().to_string()),
        ]),
        Row::new(vec![
            Cell::from("served events"),
            Cell::from(ov.served_events.to_string()),
            Cell::from("arm audits"),
            Cell::from(ov.arm_audits.to_string()),
        ]),
        Row::new(vec![
            Cell::from("displacements"),
            Cell::from(ov.displacement_events.to_string()),
            Cell::from(""),
            Cell::from(""),
        ]),
    ];
    let state_table = Table::new(
        state_rows,
        [
            Constraint::Length(14),
            Constraint::Length(12),
            Constraint::Length(14),
            Constraint::Length(12),
        ],
    )
    .header(
        Row::new(vec![
            Cell::from("State"),
            Cell::from("Count"),
            Cell::from("State"),
            Cell::from("Count"),
        ])
        .style(Style::default().add_modifier(Modifier::BOLD)),
    )
    .block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Entity state "),
    );
    f.render_widget(state_table, chunks[0]);

    // Decay histogram (active entities only).
    let decay_area = chunks[1];
    let max_bucket = ov
        .decay_buckets
        .iter()
        .map(|(_, n)| *n)
        .max()
        .unwrap_or(1)
        .max(1);
    let decay_lines: Vec<Line> = ov
        .decay_buckets
        .iter()
        .map(|(label, n)| {
            let frac = *n as f64 / max_bucket as f64;
            let bar = "█".repeat(((frac * 30.0) as usize).max(1).min(30));
            Line::from(vec![
                Span::styled(format!(" {label} "), Style::default().fg(Color::Cyan)),
                Span::raw(format!("{bar} {n}")),
            ])
        })
        .collect();
    f.render_widget(
        Paragraph::new(decay_lines).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Decay (active) "),
        ),
        decay_area,
    );

    // Top categories.
    let cat_lines: Vec<Line> = ov
        .categories
        .iter()
        .map(|(c, n)| Line::from(vec![Span::raw(format!("  {c} — {n}"))]))
        .collect();
    f.render_widget(
        Paragraph::new(cat_lines)
            .block(Block::default().borders(Borders::ALL).title(" Categories ")),
        chunks[2],
    );
}

fn draw_entities(f: &mut Frame, app: &mut App, area: Rect) {
    let chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(45), Constraint::Percentage(55)])
        .split(area);

    let mut state = ListState::default();
    state.select(Some(app.selected.min(app.entities.len().saturating_sub(1))));

    let items: Vec<ListItem> = app
        .entities
        .iter()
        .map(|e| {
            let mut spans = vec![
                Span::styled(
                    format!(" {:<22} ", truncate(&e.key, 22)),
                    Style::default().add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("{:<12} ", truncate(&e.category, 12)),
                    Style::default().fg(Color::Green),
                ),
                Span::styled(
                    format!("d{} ", fmt_decay(e.decay_score)),
                    Style::default().fg(Color::DarkGray),
                ),
            ];
            spans.extend(state_badges(e));
            ListItem::new(Line::from(spans))
        })
        .collect();

    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).title(format!(
            " Entities · filter: {} ",
            STATE_FILTERS[app.filter_idx]
        )))
        .highlight_style(
            Style::default()
                .bg(Color::DarkGray)
                .add_modifier(Modifier::BOLD),
        )
        .highlight_symbol("▶");
    f.render_stateful_widget(list, chunks[0], &mut state);

    if let Some((e, history, links)) = &app.detail {
        let mut lines: Vec<Line> = Vec::new();
        lines.push(Line::from(vec![
            Span::styled(
                format!(" {} ", e.key),
                Style::default().add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!("  {} ", e.category),
                Style::default().fg(Color::Green),
            ),
        ]));
        lines.push(Line::from(""));
        lines.push(Line::from(format!(
            " id: {}   source: {}   state: {}   layer: {}",
            e.id, e.source, e.status, e.layer
        )));
        lines.push(Line::from(format!(
            " decay: {:.2}   certainty: {:.2}   retrieval_count: {}   verified: {}   always_on: {}",
            e.decay_score, e.certainty, e.retrieval_count, e.verified, e.always_on
        )));
        lines.push(Line::from(format!(
            " created: {}   last_accessed: {}   ws: {}",
            fmt_ts(e.created_at_unix_ms),
            fmt_ts(e.last_accessed_unix_ms),
            if e.workspace_hash.is_empty() {
                "global"
            } else {
                &e.workspace_hash
            }
        )));
        lines.push(Line::from(format!(
            " epistemic: {}   efficacy: {}   visibility: {}   agent: {}",
            e.epistemic_state, e.efficacy_status, e.visibility, e.agent_id
        )));
        if !e.archive_reason.is_empty() {
            lines.push(Line::from(format!(" archive_reason: {}", e.archive_reason)));
        }
        if !e.tags.is_empty() {
            lines.push(Line::from(format!(" tags: {}", e.tags.join(", "))));
        }
        let badge = state_badges(e);
        if !badge.is_empty() {
            lines.push(Line::from(badge));
        }
        lines.push(Line::from(""));
        lines.push(Line::from(" ── body ──".to_string()));
        for body_line in e.body_plaintext.lines() {
            lines.push(Line::from(body_line.to_string()));
        }
        if !links.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(" ── links ──".to_string()));
            for l in links {
                lines.push(Line::from(format!("  {} → {}", l.rel, l.target_id)));
            }
        }
        if !history.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(" ── bi-temporal history ──".to_string()));
            for h in history {
                lines.push(Line::from(format!(
                    "  {} valid [{} → {}] recorded {} supersedes:{} superseded_by:{} archived:{}",
                    fmt_ts(h.recorded_at_unix_ms.unwrap_or(0)),
                    fmt_ts(h.valid_from_unix_ms.unwrap_or(0)),
                    h.valid_to_unix_ms
                        .map(fmt_ts)
                        .unwrap_or_else(|| "open".to_string()),
                    fmt_ts(h.invalidated_at_unix_ms.unwrap_or(0)),
                    if h.supersedes.is_empty() {
                        "—"
                    } else {
                        &h.supersedes
                    },
                    if h.superseded_by.is_empty() {
                        "—"
                    } else {
                        &h.superseded_by
                    },
                    h.archived
                )));
                let body = h.body_plaintext.trim();
                if !body.is_empty() && body != "{}" {
                    lines.push(Line::from(format!("      {}", body.replace('\n', " "))));
                }
            }
        }
        lines.push(Line::from(""));
        lines.push(Line::from(" [esc] back to list · [j/k] scroll".to_string()));
        let detail = Paragraph::new(lines)
            .block(Block::default().borders(Borders::ALL).title(" Detail "))
            .scroll((app.detail_scroll, 0));
        f.render_widget(detail, chunks[1]);
    } else {
        let hint = Paragraph::new(vec![
            Line::from(""),
            Line::from("  ↑/↓ or j/k navigate"),
            Line::from("  Enter open detail (body, links, bi-temporal history)"),
            Line::from("  f cycle state filter"),
            Line::from("  r refresh"),
            Line::from(""),
            Line::from("  Bodies are decrypted when --key-file (or"),
            Line::from("  $PERSEUS_VAULT_KEY_FILE) matches the vault key;"),
            Line::from("  ciphertext-at-rest rows are flagged instead."),
        ])
        .block(Block::default().borders(Borders::ALL).title(" Detail "));
        f.render_widget(hint, chunks[1]);
    }
}

fn draw_telemetry(f: &mut Frame, app: &App, area: Rect) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage(40),
            Constraint::Percentage(28),
            Constraint::Percentage(32),
        ])
        .split(area);

    let served_rows: Vec<Row> = app
        .served
        .iter()
        .map(|s| {
            Row::new(vec![
                Cell::from(fmt_ts(s.ts_unix_ms)),
                Cell::from(truncate(&s.mode, 8)),
                Cell::from(truncate(&s.category, 10)),
                Cell::from(truncate(&s.key, 16)),
                Cell::from(s.tokens_est.to_string()),
                Cell::from(truncate(&s.query, 30)),
            ])
        })
        .collect();
    let served = Table::new(
        served_rows,
        [
            Constraint::Length(19),
            Constraint::Length(9),
            Constraint::Length(11),
            Constraint::Length(17),
            Constraint::Length(9),
            Constraint::Min(10),
        ],
    )
    .header(
        Row::new(vec![
            Cell::from("ts"),
            Cell::from("mode"),
            Cell::from("category"),
            Cell::from("key"),
            Cell::from("tokens"),
            Cell::from("query"),
        ])
        .style(Style::default().add_modifier(Modifier::BOLD)),
    )
    .block(
        Block::default()
            .borders(Borders::ALL)
            .title(format!(" Served events ({}) ", app.served.len())),
    );
    f.render_widget(served, chunks[0]);

    let audit_rows: Vec<Row> = app
        .audits
        .iter()
        .map(|a| {
            Row::new(vec![
                Cell::from(fmt_ts(a.ts_unix_ms)),
                Cell::from(truncate(&a.mode, 10)),
                Cell::from(truncate(&a.arm, 10)),
                Cell::from(a.candidates.to_string()),
                Cell::from(a.reentry_candidates.to_string()),
                Cell::from(a.delivered.to_string()),
            ])
        })
        .collect();
    let audits = Table::new(
        audit_rows,
        [
            Constraint::Length(19),
            Constraint::Length(11),
            Constraint::Length(11),
            Constraint::Length(11),
            Constraint::Length(11),
            Constraint::Length(10),
        ],
    )
    .header(
        Row::new(vec![
            Cell::from("ts"),
            Cell::from("mode"),
            Cell::from("arm"),
            Cell::from("candidates"),
            Cell::from("re-entry"),
            Cell::from("delivered"),
        ])
        .style(Style::default().add_modifier(Modifier::BOLD)),
    )
    .block(
        Block::default()
            .borders(Borders::ALL)
            .title(format!(" Recall-arm audits ({}) ", app.audits.len())),
    );
    f.render_widget(audits, chunks[1]);

    let displ_rows: Vec<Row> = app
        .displacements
        .iter()
        .map(|d| {
            Row::new(vec![
                Cell::from(fmt_ts(d.ts_unix_ms)),
                Cell::from(truncate(&d.entity_id, 18)),
                Cell::from(truncate(&d.reason, 22)),
                Cell::from(d.was_sole_evidence.to_string()),
                Cell::from(truncate(&d.mode, 10)),
            ])
        })
        .collect();
    let displ = Table::new(
        displ_rows,
        [
            Constraint::Length(19),
            Constraint::Length(19),
            Constraint::Min(10),
            Constraint::Length(9),
            Constraint::Length(11),
        ],
    )
    .header(
        Row::new(vec![
            Cell::from("ts"),
            Cell::from("entity"),
            Cell::from("reason"),
            Cell::from("sole_evidence"),
            Cell::from("mode"),
        ])
        .style(Style::default().add_modifier(Modifier::BOLD)),
    )
    .block(Block::default().borders(Borders::ALL).title(format!(
        " Displacement events ({}) ",
        app.displacements.len()
    )));
    f.render_widget(displ, chunks[2]);
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let cut: String = s.chars().take(n.saturating_sub(1)).collect();
        format!("{cut}…")
    }
}

/// Entry point for `perseus-vault inspect`.
pub fn run_tui(db_path: &str, key_file: Option<&str>) -> Result<(), String> {
    let inspector = Inspector::open_ro(db_path, key_file)
        .map_err(|e| format!("cannot open {} read-only: {e}", db_path))?;

    enable_raw_mode().map_err(|e| format!("raw mode: {e}"))?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen).map_err(|e| format!("alt screen: {e}"))?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend).map_err(|e| format!("terminal: {e}"))?;

    let mut app = App::new(inspector);
    app.refresh();
    if app.overview.is_none() {
        let _ = terminal.flush();
        let _ = execute!(std::io::stdout(), LeaveAlternateScreen);
        let _ = disable_raw_mode();
        return Err(format!("no data loaded from {db_path}"));
    }

    let result = (|| -> Result<(), String> {
        loop {
            terminal
                .draw(|f| {
                    let size = f.area();
                    let chunks = Layout::default()
                        .direction(Direction::Vertical)
                        .constraints([
                            Constraint::Length(1),
                            Constraint::Length(3),
                            Constraint::Min(1),
                            Constraint::Length(1),
                        ])
                        .split(size);
                    draw_header(f, &app, chunks[0]);
                    draw_tabs(f, &app, chunks[1]);
                    match app.tab {
                        0 => draw_overview(f, &app, chunks[2]),
                        1 => draw_entities(f, &mut app, chunks[2]),
                        _ => draw_telemetry(f, &app, chunks[2]),
                    }
                    let status_line = if app.status == "__quit__" {
                        "quitting…".to_string()
                    } else {
                        app.status.clone()
                    };
                    f.render_widget(
                        Paragraph::new(status_line)
                            .style(Style::default().fg(Color::Gray))
                            .block(Block::default().borders(Borders::NONE)),
                        chunks[3],
                    );
                })
                .map_err(|e| format!("draw: {e}"))?;

            if app.status == "__quit__" {
                break;
            }
            if event::poll(std::time::Duration::from_millis(250))
                .map_err(|e| format!("poll: {e}"))?
            {
                match event::read().map_err(|e| format!("event: {e}"))? {
                    Event::Key(k) => {
                        if k.code == KeyCode::Char('q') && k.modifiers.is_empty() {
                            break;
                        }
                        app.handle_key(k.code);
                    }
                    Event::Resize(_, _) => {}
                    _ => {}
                }
            }
        }
        Ok(())
    })();

    let _ = terminal.flush();
    let _ = execute!(std::io::stdout(), LeaveAlternateScreen);
    let _ = disable_raw_mode();
    result
}
