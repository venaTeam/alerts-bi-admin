"""The operator console stylesheet (design section "Operator console redesign", 2026-10-19).

Served as one content-addressed file after the shared portal stylesheet, so the CSP keeps
``style-src 'self'``: pages carry no inline styles. Every rule is scoped to ``#abi-console``.
"""

CONSOLE_CSS = """
html,body{margin:0;min-height:100%;color-scheme:light}body{background:#f4f6f8}
#abi-console{--abi-bg:#f4f6f8;--abi-surface:#fff;--abi-soft:#f4f6f8;--abi-ink:#15202b;--abi-secondary:#44515e;
 --abi-muted:#5f6b78;--abi-line:#e3e7ec;--abi-line-strong:#cdd4dc;--abi-accent:#13706a;--abi-accent-ink:#0c4f4b;
 --abi-accent-soft:#e6f3f1;--abi-accent-light:#8fd0c8;--abi-side:#15202b;--abi-side-active:#263241;--abi-side-hover:#1e2a36;
 --abi-side-ink:#b4bec8;--abi-warn:#b4400b;--abi-warn-ink:#8a3108;--abi-warn-soft:#fcebdd;--abi-fail:#d9480f;
 --abi-amber:#6b4200;--abi-amber-soft:#fbf0da;--abi-amber-line:#e2c27e;--abi-danger:#b42318;--abi-danger-ink:#8f1c13;
 --abi-focus:#13706a;--abi-rule:#b3372e;--abi-rule-soft:#f7e3e0;--abi-selected:#e6f3f1;
 display:flex;flex-wrap:wrap;min-height:100vh;background:var(--abi-bg);color:var(--abi-ink);
 font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
#abi-console *{box-sizing:border-box}#abi-console [hidden]{display:none!important}
#abi-console h1,#abi-console h2,#abi-console h3,#abi-console p{margin:0}
#abi-console h1{font-size:28px;line-height:1.25;letter-spacing:-.01em;font-weight:600}
#abi-console h2{font-size:20px;line-height:1.3;font-weight:600}#abi-console h3{font-size:15px;font-weight:600}
#abi-console a{color:var(--abi-accent)}#abi-console a:hover{color:var(--abi-accent-ink)}
#abi-console button,#abi-console input,#abi-console select,#abi-console textarea{font:inherit;color:inherit}
#abi-console button,#abi-console summary,#abi-console label[for],#abi-console .abi-week-option{cursor:pointer}
#abi-console :is(a,button,input,textarea,select,summary):focus-visible{outline:3px solid var(--abi-focus);outline-offset:2px}
#abi-console input[type=date],#abi-console select,#abi-console textarea{width:100%;min-height:44px;padding:8px 12px;border:1px solid var(--abi-line-strong);border-radius:10px;background:var(--abi-surface)}
#abi-console textarea{min-height:88px;resize:vertical}
#abi-console input[type=checkbox],#abi-console input[type=radio]{width:18px;height:18px;margin:2px 0 0;accent-color:var(--abi-accent);flex:none}
#abi-console .abi-field label,#abi-console .abi-label{display:block;font-weight:500;margin-bottom:6px}
#abi-console .abi-muted{color:var(--abi-muted)}#abi-console .abi-small{font-size:14px}#abi-console .abi-tiny{font-size:13px}
#abi-console .abi-mono{font-family:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:13px;overflow-wrap:anywhere}
#abi-console .abi-sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
#abi-console .abi-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}#abi-console .abi-between{justify-content:space-between}
#abi-console .abi-stack{display:flex;flex-direction:column;gap:20px}#abi-console .abi-gap-s{gap:8px}
#abi-console .abi-inline-form{display:inline}

/* Sidebar and page frame */
#abi-console .abi-side{flex:1 0 232px;background:var(--abi-side);color:#e5eaef;padding:24px 16px;display:flex;flex-direction:column;gap:28px}
#abi-console .abi-brand{display:flex;align-items:center;gap:10px;padding:0 8px;color:#fff;text-decoration:none;font-weight:600}
#abi-console .abi-brand:hover{color:#fff}#abi-console .abi-mark{width:20px;height:20px;border-radius:6px;background:#3fb2a6}
#abi-console .abi-navs{display:flex;flex-direction:column;gap:4px}
#abi-console .abi-nav{display:flex;align-items:center;gap:12px;min-height:44px;padding:0 12px;border-radius:10px;color:var(--abi-side-ink);text-decoration:none;font-weight:500}
#abi-console .abi-nav:hover{background:var(--abi-side-hover);color:#fff}
#abi-console .abi-nav[aria-current=page]{background:var(--abi-side-active);color:#fff}
#abi-console .abi-main{flex:999 1 560px;min-width:0;padding:32px 40px 56px}
#abi-console .abi-page{max-width:960px;display:flex;flex-direction:column;gap:20px}
#abi-console .abi-page.wide{max-width:1240px}
#abi-console .abi-back{display:inline-flex;align-items:center;gap:6px;color:var(--abi-muted);text-decoration:none;font-size:14px;align-self:flex-start;min-height:32px}
#abi-console .abi-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
#abi-console .abi-sub{margin-top:4px;color:var(--abi-muted);font-size:14px}
#abi-console .abi-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}

/* Buttons, links and chips */
#abi-console .abi-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:0 16px;border-radius:10px;border:1px solid var(--abi-line-strong);background:var(--abi-surface);color:var(--abi-ink);font-weight:500;text-decoration:none;white-space:nowrap}
#abi-console .abi-btn:hover{background:var(--abi-soft);color:var(--abi-ink)}
#abi-console .abi-btn.primary{background:var(--abi-accent);border-color:var(--abi-accent);color:#fff}
#abi-console .abi-btn.primary:hover{background:var(--abi-accent-ink);border-color:var(--abi-accent-ink);color:#fff}
#abi-console .abi-btn.danger{background:var(--abi-danger);border-color:var(--abi-danger);color:#fff}
#abi-console .abi-btn.danger:hover{background:var(--abi-danger-ink);color:#fff}
#abi-console .abi-btn.small{min-height:36px;padding:0 12px;font-size:14px}
#abi-console .abi-btn:disabled{opacity:.5;cursor:not-allowed}
#abi-console .abi-link{border:0;background:none;padding:0;color:var(--abi-accent);font-weight:500;text-decoration:underline;text-underline-offset:3px;font-size:14px}
#abi-console .abi-link:hover{color:var(--abi-accent-ink)}#abi-console .abi-link.danger{color:var(--abi-danger)}
#abi-console .abi-chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:13px;font-weight:500;white-space:nowrap;background:#eef1f4;color:var(--abi-secondary)}
#abi-console .abi-chip.good,#abi-console .abi-chip.confirmed,#abi-console .abi-chip.published{background:var(--abi-accent-soft);color:var(--abi-accent-ink)}
#abi-console .abi-chip.amber,#abi-console .abi-chip.pending,#abi-console .abi-chip.held{background:var(--abi-amber-soft);color:var(--abi-amber)}
#abi-console .abi-chip.warn,#abi-console .abi-chip.failed{background:var(--abi-warn-soft);color:var(--abi-warn)}
#abi-console .abi-chip.none{background:var(--abi-surface);color:var(--abi-muted);border:1px solid var(--abi-line-strong)}

/* Messages and notes */
#abi-console .abi-flash{display:flex;gap:12px;align-items:flex-start;padding:14px 18px;border-radius:12px;background:var(--abi-accent-soft);color:var(--abi-accent-ink);font-size:14px}
#abi-console .abi-flash.error{background:var(--abi-warn-soft);color:var(--abi-warn-ink)}
#abi-console .abi-note{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;padding:14px 18px;border-radius:12px;background:var(--abi-amber-soft);color:#5c3900;font-size:14px}
#abi-console .abi-note.good{background:var(--abi-accent-soft);color:var(--abi-accent-ink)}
#abi-console .abi-note.warn{background:var(--abi-warn-soft);color:var(--abi-warn-ink)}
#abi-console .abi-note p{flex:1 1 320px}#abi-console .abi-note a{color:inherit;font-weight:500}
#abi-console .abi-note svg{flex:none;margin-top:2px}

/* Panels */
#abi-console .abi-panel{background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:14px;padding:28px;min-width:0;display:flex;flex-direction:column;gap:18px}
#abi-console .abi-panel.flush{padding:0;gap:0}#abi-console .abi-panel.tight{padding:20px 24px;gap:14px}
#abi-console .abi-panel-foot{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-top:18px;border-top:1px solid #edf0f3}
#abi-console .abi-empty{align-items:center;text-align:center;padding:56px 28px}
#abi-console .abi-empty-icon{width:56px;height:56px;border-radius:16px;background:var(--abi-accent-soft);color:var(--abi-accent);display:grid;place-items:center}
#abi-console .abi-empty p{max-width:460px;color:var(--abi-muted)}
#abi-console .abi-error-code{font-size:48px;font-weight:600;color:var(--abi-muted)}

/* Teams */
#abi-console .abi-filter{display:flex;padding:4px;gap:4px;background:#e7ebef;border-radius:12px}
#abi-console .abi-filter a{display:inline-flex;align-items:center;min-height:40px;padding:0 16px;border-radius:9px;color:var(--abi-secondary);text-decoration:none;font-weight:500}
#abi-console .abi-filter a[aria-current=page]{background:var(--abi-surface);color:var(--abi-ink)}
#abi-console .abi-cards{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
#abi-console .abi-card{display:flex;flex-direction:column;gap:18px;min-height:156px;height:100%;padding:20px;background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:14px;color:var(--abi-ink);text-decoration:none}
#abi-console .abi-card:hover{border-color:#8c97a3;color:var(--abi-ink)}
#abi-console .abi-card-name{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:17px;font-weight:600;overflow-wrap:anywhere}
#abi-console .abi-card-tag{font-size:12px;font-weight:500;color:var(--abi-muted)}
#abi-console .abi-segs{display:flex;flex-direction:column;gap:6px}
#abi-console .abi-seg-bar{display:flex;gap:4px}
#abi-console .abi-seg{flex:1 1 0;height:6px;border-radius:3px;background:var(--abi-line)}
#abi-console .abi-seg.done{background:var(--abi-accent)}#abi-console .abi-seg.current{background:var(--abi-accent-light)}#abi-console .abi-seg.fail{background:var(--abi-fail)}
#abi-console .abi-seg-names{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;font-size:12px;color:var(--abi-muted)}
#abi-console .abi-card-status{margin-top:auto;font-size:14px;font-weight:500}
#abi-console .tone-action{color:var(--abi-accent)}#abi-console .tone-warn{color:var(--abi-warn)}#abi-console .tone-muted{color:var(--abi-muted)}

/* Week picker */
#abi-console .abi-picker{position:relative}
#abi-console .abi-picker>summary{list-style:none;display:inline-flex;align-items:center;gap:10px;min-height:44px;padding:0 14px;border-radius:10px;border:1px solid var(--abi-line-strong);background:var(--abi-surface);font-weight:500}
#abi-console .abi-picker>summary::-webkit-details-marker{display:none}
#abi-console .abi-picker[open]>summary{border:2px solid var(--abi-accent);padding:0 13px}
#abi-console .abi-pop{position:absolute;z-index:5;top:52px;right:0;width:360px;max-width:calc(100vw - 32px);background:var(--abi-surface);border:1px solid var(--abi-line-strong);border-radius:14px;box-shadow:0 12px 32px #15202b29;padding:8px}
#abi-console .abi-pop-title{padding:8px 12px 4px;font-size:13px;color:var(--abi-muted)}
#abi-console .abi-pop ul{list-style:none;margin:0;padding:0;max-height:440px;overflow:auto}
#abi-console .abi-pop li a{display:flex;align-items:center;gap:12px;min-height:44px;padding:4px 12px;border-radius:10px;color:var(--abi-ink);text-decoration:none}
#abi-console .abi-pop li a:hover{background:var(--abi-soft)}#abi-console .abi-pop li a[aria-current=true]{background:var(--abi-accent-soft)}
#abi-console .abi-pop .abi-pop-week{flex:1 1 auto;font-weight:500}#abi-console .abi-pop .abi-pop-state{font-size:13px;color:var(--abi-muted)}
#abi-console .abi-pop-foot{display:flex;justify-content:space-between;gap:12px;padding:10px 12px 6px;margin-top:4px;border-top:1px solid #edf0f3;font-size:14px}
#abi-console .abi-dot{flex:none;width:10px;height:10px;border-radius:50%;background:var(--abi-line-strong)}
#abi-console .abi-dot.published{background:var(--abi-accent)}#abi-console .abi-dot.failed{background:var(--abi-fail)}
#abi-console .abi-dot.unpublished,#abi-console .abi-dot.held,#abi-console .abi-dot.withdrawn{background:#e0a526}

/* Steps */
#abi-console .abi-steps{list-style:none;margin:0;padding:8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:14px}
#abi-console .abi-step{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border-radius:10px}
#abi-console .abi-step[aria-current=step]{background:var(--abi-accent-soft)}#abi-console .abi-step.fail{background:var(--abi-warn-soft)}
#abi-console .abi-step-dot{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-weight:600;font-size:14px;border:2px solid #b8c1cb;color:var(--abi-muted)}
#abi-console .abi-step.done .abi-step-dot{background:var(--abi-accent);border-color:var(--abi-accent);color:#fff}
#abi-console .abi-step[aria-current=step] .abi-step-dot{border-color:var(--abi-accent);color:var(--abi-accent)}
#abi-console .abi-step.fail .abi-step-dot{background:var(--abi-fail);border-color:var(--abi-fail);color:#fff}
#abi-console .abi-step.running .abi-step-dot{border:3px solid #bfe3de;border-top-color:var(--abi-accent);animation:abi-spin 1s linear infinite}
#abi-console .abi-step-name{display:block;font-weight:600}#abi-console .abi-step.todo .abi-step-name{color:var(--abi-secondary)}
#abi-console .abi-step-text{display:block;color:var(--abi-muted);font-size:14px}
#abi-console .abi-step[aria-current=step] .abi-step-text{color:var(--abi-secondary)}#abi-console .abi-step.fail .abi-step-text{color:var(--abi-warn-ink)}
@keyframes abi-spin{to{transform:rotate(360deg)}}
@keyframes abi-slide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
#abi-console .abi-indeterminate{height:8px;border-radius:4px;background:var(--abi-line);overflow:hidden}
#abi-console .abi-indeterminate span{display:block;width:40%;height:8px;border-radius:4px;background:var(--abi-accent);animation:abi-slide 1.6s ease-in-out infinite}
@media(prefers-reduced-motion:reduce){#abi-console .abi-step.running .abi-step-dot,#abi-console .abi-indeterminate span{animation:none}}

/* Totals, facts and lists */
#abi-console .abi-totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
#abi-console .abi-total{padding:14px 16px;border:1px solid var(--abi-line);border-radius:12px}
#abi-console .abi-total-name{font-size:13px;color:var(--abi-muted)}
#abi-console .abi-total-line{margin-top:2px;color:var(--abi-muted)}#abi-console .abi-total-line strong{font-size:20px;font-weight:600;color:var(--abi-ink)}
#abi-console .abi-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin:0;padding:20px;border-radius:12px;background:var(--abi-soft)}
#abi-console .abi-stats div{display:flex;flex-direction:column-reverse}#abi-console .abi-stats dt{color:var(--abi-muted);font-size:14px}
#abi-console .abi-stats dd{margin:0;font-size:26px;font-weight:600}
#abi-console .abi-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:0}
#abi-console .abi-facts dt{color:var(--abi-muted);font-size:13px}#abi-console .abi-facts dd{margin:0;font-size:14px;overflow-wrap:anywhere}
#abi-console .abi-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--abi-line)}
#abi-console .abi-list>li{border-bottom:1px solid var(--abi-line)}
#abi-console .abi-item{display:flex;align-items:center;gap:16px;padding:14px 8px;color:var(--abi-ink);text-decoration:none}
#abi-console a.abi-item:hover{background:#f7f9fa;color:var(--abi-ink)}
#abi-console .abi-item-main{flex:1 1 auto;min-width:0}
#abi-console .abi-item-title{display:block;font-weight:500;overflow-wrap:anywhere}
#abi-console .abi-item-sub{display:block;color:var(--abi-muted);font-size:14px;overflow-wrap:anywhere}
#abi-console .abi-item-note{display:block;margin-top:6px;color:var(--abi-secondary);font-size:14px;overflow-wrap:anywhere}
#abi-console .abi-item-chips{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
#abi-console .abi-item-when{flex:none;color:var(--abi-muted);font-size:13px}
#abi-console .abi-pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;font-size:14px;color:var(--abi-muted)}
#abi-console .abi-pager span:last-child{display:flex;gap:16px}

/* Details sections */
#abi-console details.abi-more{background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:14px}
#abi-console details.abi-more>summary{padding:16px 24px;font-weight:500;color:var(--abi-secondary);min-height:24px}
#abi-console .abi-more-body{padding:0 24px 24px;display:flex;flex-direction:column;gap:20px}
#abi-console details.abi-sub-more>summary{font-weight:500;color:var(--abi-secondary);font-size:14px}
#abi-console details.abi-sub-more[open]>summary{margin-bottom:12px}
#abi-console .abi-activity{list-style:none;margin:0;padding:0;font-size:14px}
#abi-console .abi-activity li{display:flex;justify-content:space-between;gap:4px 12px;flex-wrap:wrap;padding:9px 0;border-bottom:1px solid #edf0f3}
#abi-console .abi-activity li:last-child{border-bottom:0}#abi-console .abi-activity .abi-activity-detail{flex-basis:100%;color:var(--abi-muted)}
#abi-console .abi-pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 "IBM Plex Mono",ui-monospace,monospace;background:var(--abi-soft);border-radius:10px;padding:12px;max-height:320px;overflow:auto;margin:0}
#abi-console .abi-message{padding:14px 16px;border-radius:10px;background:var(--abi-soft);font-family:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:14px;overflow-wrap:anywhere}

/* Finding page */
#abi-console h2.abi-h3{font-size:16px}#abi-console .abi-finding-title{margin-top:10px;font-size:26px}
#abi-console .abi-split{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start}
#abi-console .abi-split-main{flex:2 1 420px;min-width:0;display:flex;flex-direction:column;gap:20px}
#abi-console .abi-split-side{flex:1 1 280px;min-width:0}
#abi-console .abi-history{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
#abi-console .abi-history li{padding-top:14px;border-top:1px solid #edf0f3}

/* Multi-week progress */
#abi-console progress.abi-bar{appearance:none;display:block;width:100%;height:8px;border:0;border-radius:4px;background:var(--abi-line);overflow:hidden;accent-color:var(--abi-accent)}
#abi-console progress.abi-bar::-webkit-progress-bar{background:var(--abi-line)}#abi-console progress.abi-bar::-webkit-progress-value{background:var(--abi-accent)}
#abi-console progress.abi-bar::-moz-progress-bar{background:var(--abi-accent)}
#abi-console .abi-week-state{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;border:2px solid #b8c1cb;color:var(--abi-muted);font-size:12px;font-weight:700}
#abi-console .abi-week-state.completed{background:var(--abi-accent);border-color:var(--abi-accent);color:#fff}
#abi-console .abi-week-state.running{border:3px solid #bfe3de;border-top-color:var(--abi-accent);animation:abi-spin 1s linear infinite}
#abi-console .abi-week-state.failed{background:var(--abi-fail);border-color:var(--abi-fail);color:#fff}
#abi-console .abi-week-state.cancelled{border-style:dashed}

/* Schedule */
#abi-console .abi-table-wrap{overflow-x:auto;background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:14px}
#abi-console .abi-table{width:100%;min-width:620px;border-collapse:collapse}
#abi-console .abi-table caption{text-align:left;padding:18px 24px 6px;font-size:17px;font-weight:600}
#abi-console .abi-table th{text-align:left;font-weight:500;font-size:13px;color:var(--abi-muted);padding:10px 12px}
#abi-console .abi-table td{padding:14px 12px;border-top:1px solid #edf0f3;vertical-align:top}
#abi-console .abi-table :is(th,td):first-child{padding-left:24px}#abi-console .abi-table :is(th,td):last-child{padding-right:24px}
#abi-console .abi-table a{color:var(--abi-ink);font-weight:600;text-decoration:none}#abi-console .abi-table a:hover{text-decoration:underline}
#abi-console .abi-next{display:flex;flex-direction:row;align-items:center;gap:18px;flex-wrap:wrap}
#abi-console .abi-next-icon{flex:none;width:44px;height:44px;border-radius:12px;background:var(--abi-accent-soft);color:var(--abi-accent);display:grid;place-items:center}
#abi-console .abi-next>div{flex:1 1 260px}#abi-console .abi-next strong{display:block;font-size:18px;font-weight:600}

/* Filters on the Decisions page */
#abi-console .abi-filters{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
#abi-console .abi-filters .abi-field{min-width:170px}#abi-console .abi-filters .abi-field label{font-size:13px;color:var(--abi-muted)}

/* Dialogs */
#abi-console .abi-dialog{width:580px;max-width:calc(100% - 32px);max-height:calc(100vh - 32px);border:0;border-radius:16px;padding:0;background:var(--abi-surface);color:var(--abi-ink);box-shadow:0 24px 64px #15202b47}
#abi-console .abi-dialog::backdrop{background:#15202b80}
#abi-console .abi-dialog form{display:flex;flex-direction:column;max-height:calc(100vh - 32px)}
#abi-console .abi-dialog-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:22px 24px 0}
#abi-console .abi-dialog-body{padding:16px 24px 8px;display:flex;flex-direction:column;gap:18px;overflow:auto}
#abi-console .abi-dialog-foot{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap;padding:20px 24px 24px}
#abi-console .abi-dialog-foot .abi-foot-note{flex:1 1 220px;color:var(--abi-muted);font-size:14px}
#abi-console .abi-close{width:40px;height:40px;border-radius:10px;border:0;background:none;display:grid;place-items:center;color:var(--abi-secondary)}
#abi-console .abi-close:hover{background:var(--abi-soft)}
#abi-console .abi-context{padding:14px 16px;border-radius:12px;background:var(--abi-soft)}
#abi-console .abi-context strong{display:block;font-weight:600}
#abi-console fieldset{margin:0;padding:0;border:0;min-width:0}#abi-console legend{padding:0;font-weight:500}
#abi-console .abi-weeks{list-style:none;margin:6px 0 0;padding:0;border:1px solid var(--abi-line);border-radius:12px;overflow:hidden}
#abi-console .abi-weeks li+li{border-top:1px solid #edf0f3}
#abi-console .abi-week-option{display:flex;align-items:center;gap:14px;min-height:52px;padding:8px 16px;margin:0}
#abi-console .abi-week-option:hover{background:#f7f9fa}
#abi-console .abi-week-name{flex:1 1 auto;font-weight:500}
#abi-console .abi-week-note{font-size:13px;color:var(--abi-muted);text-align:right}#abi-console .abi-week-note.warn{color:#8a5300}
#abi-console .abi-choices{display:flex;flex-direction:column;gap:8px;margin-top:8px}
#abi-console .abi-choice{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:12px;border:1px solid var(--abi-line-strong)}
#abi-console .abi-choice:has(input:checked){border-color:var(--abi-accent);background:var(--abi-accent-soft);box-shadow:inset 0 0 0 1px var(--abi-accent)}
#abi-console .abi-check{display:flex;gap:12px;align-items:flex-start}

/* Legacy detailed analysis rendered by the shared summary view */
#abi-console .abi-legacy{display:block;min-width:0;overflow:hidden;font-size:14px}
#abi-console .abi-legacy .sl-scroll{width:100%;max-width:100%;margin-left:0}
#abi-console .abi-legacy :is(.section-h,.block,.tools,.pager){padding:16px}
#abi-console .abi-legacy :is(.links,.inline){display:flex;flex-wrap:wrap;gap:8px}
#abi-console .abi-legacy .button{display:inline-flex;padding:8px 12px;border:1px solid var(--abi-line);border-radius:8px;background:var(--abi-surface);font-size:13px}
#abi-console .abi-legacy .card{background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:10px;margin-top:16px}
#abi-console .abi-legacy .table-wrap{overflow:auto}#abi-console .abi-legacy .sub{font-size:13px;color:var(--abi-muted)}
#abi-console .abi-legacy mark.sup{background:var(--abi-rule-soft);color:var(--abi-rule)}
#abi-console .abi-legacy pre.sql{white-space:pre-wrap;overflow-wrap:anywhere}
#abi-console .abi-legacy .panels{list-style:none;padding:0}#abi-console .abi-legacy .panels li{margin:12px 0}
#abi-console .abi-legacy h2{font-size:17px}

/* Narrow screens: the sidebar becomes a top strip */
@media(max-width:760px){
 #abi-console .abi-side{flex-basis:100%;flex-direction:row;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:12px 16px}
 #abi-console .abi-navs{flex-direction:row;flex-wrap:wrap;gap:4px}
 #abi-console .abi-nav{padding:0 10px}#abi-console .abi-nav svg{display:none}
 #abi-console .abi-main{padding:20px 16px 40px}
 #abi-console .abi-panel{padding:20px}#abi-console .abi-more-body{padding:0 16px 20px}
 #abi-console details.abi-more>summary{padding:16px}
 #abi-console h1{font-size:24px}
 #abi-console .abi-pop{left:0;right:auto}
 #abi-console .abi-item{flex-wrap:wrap}#abi-console .abi-item-chips{justify-content:flex-start}
}
@media(pointer:coarse){#abi-console input,#abi-console select,#abi-console textarea{font-size:16px}}
"""

__all__ = ["CONSOLE_CSS"]
