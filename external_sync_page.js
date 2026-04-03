// ══════════════════════════════════════════════
// EXTERNAL SYNC PAGE — Apple Card + Synchrony
// ══════════════════════════════════════════════
function ExternalSyncPage() {
  const [appleSession, setAppleSession] = useState(null);
  const [appleStatus, setAppleStatus]   = useState(null);
  const [apple2fa, setApple2fa]         = useState('');
  const [appleLoading, setAppleLoading] = useState(false);
  const [appleError, setAppleError]     = useState('');
  const [appleDateFrom, setAppleDateFrom] = useState(()=>{
    const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-01`;
  });
  const [appleDateTo, setAppleDateTo] = useState(()=>{
    const d=new Date();return d.toISOString().split('T')[0];
  });
  const [syncSession, setSyncSession]   = useState(null);
  const [syncStatus, setSyncStatus]     = useState(null);
  const [syncLoading, setSyncLoading]   = useState(false);
  const [syncError, setSyncError]       = useState('');
  const pollRef = useRef(null);

  function stopPolling() {
    if(pollRef.current){clearInterval(pollRef.current);pollRef.current=null;}
  }
  useEffect(()=>()=>stopPolling(),[]);

  function startPolling(sessionId, setStatus, setError, setLoading) {
    stopPolling();
    pollRef.current = setInterval(async ()=>{
      try {
        const data = await apiFetch(`/api/scrape/status/${sessionId}`);
        setStatus(data);
        if(['complete','error','not_implemented'].includes(data.status)){
          stopPolling();setLoading(false);
          if(data.status==='error') setError(data.error||'Unknown error');
        }
      } catch(e){stopPolling();setLoading(false);setError(e.message);}
    }, 2000);
  }

  async function startApple() {
    setAppleLoading(true);setAppleError('');setAppleStatus(null);
    try {
      const data = await apiFetch('/api/scrape/apple/start',{method:'POST',body:{
        start_date:appleDateFrom, end_date:appleDateTo, backfill:false,
      }});
      setAppleSession(data.session_id);
      startPolling(data.session_id, setAppleStatus, setAppleError, setAppleLoading);
    } catch(e){setAppleError(e.message);setAppleLoading(false);}
  }

  async function submitApple2fa() {
    if(apple2fa.length!==6)return;
    setAppleError('');
    try {
      await apiFetch('/api/scrape/apple/verify',{method:'POST',body:{
        session_id:appleSession, code:apple2fa,
      }});
      setApple2fa('');
    } catch(e){setAppleError(e.message);}
  }

  async function startSync() {
    setSyncLoading(true);setSyncError('');setSyncStatus(null);
    try {
      const data = await apiFetch('/api/scrape/synchrony/start',{method:'POST',body:{}});
      setSyncSession(data.session_id);
      startPolling(data.session_id, setSyncStatus, setSyncError, setSyncLoading);
    } catch(e){setSyncError(e.message);setSyncLoading(false);}
  }

  const statusColor = s => {
    if(!s)return'var(--t3)';
    if(s==='complete')return'var(--green)';if(s==='error')return'var(--red)';
    if(s==='awaiting_2fa')return'var(--amber)';return'var(--blue)';
  };
  const statusLabel = s => {
    if(!s)return'idle';
    const map={starting:'Starting...',logging_in:'Logging in...',awaiting_2fa:'Waiting for 2FA code',
      verifying_2fa:'Verifying code...',authenticated:'Authenticated',
      scraping_balance:'Scraping balance...',scraping_transactions:'Scraping transactions...',
      importing:'Importing data...',complete:'Complete',error:'Error',not_implemented:'Not yet implemented'};
    return map[s]||s;
  };

  return html`
    <div style=${{display:'flex',flexDirection:'column',gap:'14px'}}>
      <div className="two-col">
        <div className="panel">
          <div className="ph">
            <span className="ph-title" style=${{color:'var(--t1)'}}>Apple Card</span>
            <span className="ph-stat">Semi-automated · SMS 2FA required</span>
          </div>
          <div style=${{padding:'14px',display:'flex',flexDirection:'column',gap:'12px'}}>
            <div className="form-row">
              <div className="fg"><label className="fl">Start Date</label>
                <input className="fi" type="date" value=${appleDateFrom} onChange=${e=>setAppleDateFrom(e.target.value)} style=${{width:'160px'}}/></div>
              <div className="fg"><label className="fl">End Date</label>
                <input className="fi" type="date" value=${appleDateTo} onChange=${e=>setAppleDateTo(e.target.value)} style=${{width:'160px'}}/></div>
              <button className="btn btn-p" onClick=${startApple} disabled=${appleLoading}>
                ${appleLoading?html`<span className="spin">↻</span> running...`:'Sync Apple Card'}</button>
            </div>
            ${appleStatus&&html`
              <div style=${{padding:'10px',background:'var(--s2)',border:'1px solid var(--ln2)',borderRadius:'var(--r2)'}}>
                <div style=${{display:'flex',alignItems:'center',gap:'8px',marginBottom:'6px'}}>
                  <div style=${{width:'8px',height:'8px',borderRadius:'50%',background:statusColor(appleStatus.status)}}/>
                  <span style=${{fontFamily:'var(--mono)',fontSize:'11px',color:statusColor(appleStatus.status)}}>${statusLabel(appleStatus.status)}</span>
                </div>
                ${appleStatus.balance!=null&&html`<div style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--t2)',marginBottom:'4px'}}>Balance: $${appleStatus.balance} · Available: $${appleStatus.available||'—'}</div>`}
                ${appleStatus.txn_count>0&&html`<div style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--green)'}}>✓ ${appleStatus.txn_count} transactions imported</div>`}
              </div>`}
            ${appleStatus&&appleStatus.status==='awaiting_2fa'&&html`
              <div style=${{padding:'12px',background:'var(--ad)',border:'1px solid var(--ab)',borderRadius:'var(--r2)'}}>
                <div style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--amber)',marginBottom:'8px',textTransform:'uppercase',letterSpacing:'1px'}}>Enter SMS verification code</div>
                <div className="form-row">
                  <input className="fi" placeholder="6-digit code" value=${apple2fa}
                    onInput=${e=>setApple2fa(e.target.value.replace(/\\D/g,'').slice(0,6))}
                    onKeyDown=${e=>e.key==='Enter'&&submitApple2fa()}
                    style=${{width:'140px',fontSize:'18px',fontFamily:'var(--mono)',letterSpacing:'4px',textAlign:'center'}} autoFocus maxLength="6"/>
                  <button className="btn btn-s" onClick=${submitApple2fa} disabled=${apple2fa.length!==6}>Submit Code</button>
                </div>
              </div>`}
            ${appleError&&html`<div className="err">${appleError}</div>`}
          </div>
        </div>
        <div className="panel">
          <div className="ph">
            <span className="ph-title" style=${{color:'var(--t1)'}}>Synchrony</span>
            <span className="ph-stat">Automated · runs hourly + on-demand</span>
          </div>
          <div style=${{padding:'14px',display:'flex',flexDirection:'column',gap:'12px'}}>
            <div style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--t3)',marginBottom:'4px'}}>Discount Tire (···5339) + Amazon Prime Store Card (···8814)</div>
            <div><button className="btn btn-p" onClick=${startSync} disabled=${syncLoading}>
              ${syncLoading?html`<span className="spin">↻</span> running...`:'Sync Synchrony Accounts'}</button></div>
            ${syncStatus&&html`
              <div style=${{padding:'10px',background:'var(--s2)',border:'1px solid var(--ln2)',borderRadius:'var(--r2)'}}>
                <div style=${{display:'flex',alignItems:'center',gap:'8px',marginBottom:'6px'}}>
                  <div style=${{width:'8px',height:'8px',borderRadius:'50%',background:statusColor(syncStatus.status)}}/>
                  <span style=${{fontFamily:'var(--mono)',fontSize:'11px',color:statusColor(syncStatus.status)}}>${statusLabel(syncStatus.status)}</span>
                </div>
                ${syncStatus.accounts&&syncStatus.accounts.length>0&&html`<div style=${{marginBottom:'4px'}}>
                  ${syncStatus.accounts.map((a,i)=>html`<div key=${i} style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--t2)'}}>···${a.last_four}: $${a.balance||'—'} balance · $${a.available||'—'} available</div>`)}</div>`}
                ${syncStatus.txn_count>0&&html`<div style=${{fontFamily:'var(--mono)',fontSize:'10px',color:'var(--green)'}}>✓ ${syncStatus.txn_count} transactions imported</div>`}
              </div>`}
            ${syncError&&html`<div className="err">${syncError}</div>`}
          </div>
        </div>
      </div>
    </div>`;
}