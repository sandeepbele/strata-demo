import { useEffect, useState, type FormEvent, type FocusEvent, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'

type Project = { id: string; name: string; organization: string; kind: string; dockets: string[]; obligations_file: string }
type Obligation = { id: string; title: string; text: string }
type Source = { file: string; original_filename?: string; filing_id?: string; status: string; date: string; title?: string; decision?: string; source_url: string | null; revises?: string | null; uploaded?: boolean }
type Docket = { docket_id: string; agency: string; title: string; versions: Record<string, Source>; comparisons: { old: string; new: string; legacy_direct?: boolean; manual_upload?: boolean; backfill?: boolean }[] }
type ProjectDetail = Project & { obligations: Obligation[]; docket_details: Docket[] }
type Citation = { change_id: string; side: 'old' | 'new'; version: string; status: string; pdf_page_start: number | null; quote: string }
type Assessment = { obligation_id: string; outcome: 'affected' | 'needs_review' | 'not_affected'; summary: string; rationale: string; proposed_action: string; reviewer_role: string; open_questions: string[]; citations: Citation[]; review_policy_version?: number }
type Result = { obligation_id: string; status: 'complete' | 'failed'; assessment?: Assessment; error?: string }
type DecisionAction = 'accept' | 'reject' | 'route_to_legal'
type Decision = { id: string; run_id: string | null; obligation_id: string; action: DecisionAction; actor_label: string; decided_at: string }
type ReviewState = { status: 'idle' | 'reviewing' | 'completed' | 'partial' | 'failed' | 'interrupted'; completed: number; total: number; started_at: string | null; finished_at: string | null; error: string | null; run_id: string | null }
type ReviewRun = { run_id: string | null; trigger: 'new_version' | 'manual_upload' | 'obligation_added' | 'retry' | 'unknown'; obligation_version?: string; obligation_ids?: string[] | null; comparison: { docket_id: string; old: string; new: string } | null; review: ReviewState; results: Result[] }
type Workflow = { project_id: string; visible_version: string | null; visible_versions: string[]; obligation_version: string; review: ReviewState; results: Result[]; runs: ReviewRun[]; decisions: Decision[] }
type Side = { version: string; status: string; page_start: number | null; text: string; context: string; text_truncated: boolean; locations: { page: number; line: number }[] }
type ChangeDetail = { change_id: string; kind: string; alignment: string; old: Side; new: Side; diff_lines: { type: 'meta' | 'removed' | 'added' | 'context'; text: string }[]; diff_truncated: boolean }
type SourcePage = { version: string; status: string; page: number; page_count: number; lines: string[] }
type View = 'artifacts' | 'docket'
type Artifact = 'obligations' | 'project'
type ChangeSide = 'old' | 'new'
type Guide = { text: string; left: number; top: number; above: boolean }

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

const params = new URLSearchParams(window.location.search)
const initialView: View = params.get('view') === 'docket' ? 'docket' : 'artifacts'
const initialProject = params.get('project') || ''
const initialChange = params.get('change') || ''
const initialOld = params.get('old') || ''
const initialNew = params.get('new') || ''
const initialSide: ChangeSide = params.get('side') === 'old' ? 'old' : 'new'
const compact = (value: string, limit = 160) => {
  const text = value.replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text
}
const stamp = (value: string) => new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
const sourceLabel = (source?: Source) => source?.original_filename || source?.file.split('/').at(-1) || source?.title || 'PDF'
const statusLabel = (source?: Source) => source?.status === 'draft_proposal' ? 'Draft proposal' : source?.status === 'final' ? 'Final' : 'Proposed'
const revisionLabel = (docket: Docket, visibleVersions: string[], version: string) => {
  const filingId = docket.versions[version]?.filing_id
  if (!filingId) return null
  const siblings = visibleVersions.filter(item => docket.versions[item]?.filing_id === filingId)
  return siblings.length > 1 ? `v${siblings.indexOf(version) + 1}` : null
}
const displayVersion = (docket: Docket, visibleVersions: string[], version: string) =>
  revisionLabel(docket, visibleVersions, version) || sourceLabel(docket.versions[version])
const decisionLabel: Record<DecisionAction, string> = { accept: 'Accepted', reject: 'Rejected', route_to_legal: 'Routed to Legal' }
const decisionsFor = (workflow: Workflow, runId: string | null, obligationId: string) => workflow.decisions.filter(item => item.run_id === runId && item.obligation_id === obligationId)
function urlFor(projectId: string, view: View, changeId = '', side: ChangeSide = 'new', comparison?: { old: string; new: string } | null) {
  const next = new URLSearchParams({ project: projectId, view })
  if (changeId) next.set('change', changeId)
  if (changeId && side === 'old') next.set('side', 'old')
  if (changeId && comparison) { next.set('old', comparison.old); next.set('new', comparison.new) }
  return `?${next}${changeId ? '#source-page' : ''}`
}
function parseObligation(text: string) {
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean)
  const metadata = lines.filter(line => /^(Owner|Status|Company record):/.test(line))
  const body = lines.filter(line => !/^(Owner|Status|Company record):/.test(line))
  return { metadata, body }
}

function proposedActionParts(value: string) {
  const markers = Array.from(value.matchAll(/(?:^|\s)(?:\((\d{1,2})\)|(\d{1,2})[.)])\s+/g))
  const steps: RegExpMatchArray[] = []
  for (const marker of markers) {
    if (Number(marker[1] || marker[2]) === steps.length + 1) steps.push(marker)
  }
  if (steps.length > 1) {
    return {
      intro: value.slice(0, steps[0].index).trim().replace(/:$/, ''),
      items: steps.map((match, index) => value.slice((match.index || 0) + match[0].length, steps[index + 1]?.index ?? value.length).trim().replace(/;$/, '')),
    }
  }
  const lines = value.split(/\n+/).map(line => line.trim()).filter(Boolean)
  if (lines.length > 1 && lines.every(line => /^[-•]\s+/.test(line))) {
    return { intro: '', items: lines.map(line => line.replace(/^[-•]\s+/, '')) }
  }
  return { intro: '', items: [value.trim()] }
}

function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState(initialProject)
  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [view, setView] = useState<View>(initialView)
  const [artifact, setArtifact] = useState<Artifact>('obligations')
  const [findingId, setFindingId] = useState('')
  const [changeId, setChangeId] = useState(initialChange)
  const [changeSide, setChangeSide] = useState<ChangeSide>(initialSide)
  const [change, setChange] = useState<ChangeDetail | null>(null)
  const [sourceVersion, setSourceVersion] = useState('v1')
  const [sourcePage, setSourcePage] = useState(1)
  const [pageData, setPageData] = useState<SourcePage | null>(null)
  const [busy, setBusy] = useState(false)
  const [decisionBusy, setDecisionBusy] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadMode, setUploadMode] = useState<'new' | 'revision'>('new')
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadTitle, setUploadTitle] = useState('')
  const [uploadDate, setUploadDate] = useState('')
  const [uploadStatus, setUploadStatus] = useState('draft_proposal')
  const [uploadRevises, setUploadRevises] = useState('')
  const [obligationOpen, setObligationOpen] = useState(false)
  const [obligationTitle, setObligationTitle] = useState('')
  const [obligationText, setObligationText] = useState('')
  const [newObligationId, setNewObligationId] = useState('')
  const [error, setError] = useState('')
  const [guide, setGuide] = useState<Guide | null>(null)
  const selectedRun = workflow?.runs.find(run => run.run_id === selectedRunId) || workflow?.runs.at(-1)
  const activeComparison = selectedRun?.comparison

  useEffect(() => {
    api<Project[]>('/api/projects').then(items => {
      setProjects(items)
      if (!items.some(item => item.id === projectId)) setProjectId(items[0]?.id || '')
    }).catch(err => setError(err.message))
  }, [])

  useEffect(() => {
    if (!projectId) return
    let live = true
    setProject(null); setWorkflow(null); setArtifact('obligations'); setFindingId(''); setResetOpen(false); setUploadOpen(false); setObligationOpen(false); setSourcePage(1); setPageData(null); setError('')
    Promise.all([
      api<ProjectDetail>(`/api/projects/${projectId}`),
      api<Workflow>(`/api/projects/${projectId}/workflow`),
    ]).then(([detail, state]) => {
      if (live) { setProject(detail); setWorkflow(state); setSourceVersion(state.visible_version || 'v1'); setSelectedRunId((state.runs.find(run => run.comparison?.old === initialOld && run.comparison?.new === initialNew) || state.runs.at(-1))?.run_id || null) }
    }).catch(err => { if (live) setError(err.message) })
    return () => { live = false }
  }, [projectId])

  useEffect(() => {
    if (!newObligationId || !project?.obligations.some(item => item.id === newObligationId)) return
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(newObligationId)?.scrollIntoView({ block: 'center' })
      setNewObligationId('')
    })
    return () => window.cancelAnimationFrame(frame)
  }, [newObligationId, project])

  useEffect(() => {
    if (!project || !workflow || !changeId) return
    if (!workflow.runs.some(run => run.comparison && workflow.visible_versions.includes(run.comparison.new))) {
      setChangeId('')
      window.history.replaceState({}, '', urlFor(projectId, view))
    }
  }, [project, workflow?.visible_version, changeId, projectId, view])

  useEffect(() => {
    if (!projectId || workflow?.review.status !== 'reviewing') return
    const timer = window.setInterval(() => {
      api<Workflow>(`/api/projects/${projectId}/workflow`).then(setWorkflow).catch(err => setError(err.message))
    }, 1400)
    return () => window.clearInterval(timer)
  }, [projectId, workflow?.review.status])

  useEffect(() => {
    if (!projectId || !changeId || view !== 'docket' || !activeComparison) {
      setChange(null); return
    }
    let live = true
    api<ChangeDetail>(`/api/projects/${projectId}/changes/${changeId}?old=${activeComparison.old}&new=${activeComparison.new}`).then(detail => {
      if (live) setChange(detail)
    }).catch(err => { if (live) setError(err.message) })
    return () => { live = false }
  }, [projectId, changeId, view, workflow?.visible_version, activeComparison?.old, activeComparison?.new])

  useEffect(() => {
    if (!change || change.change_id !== changeId || view !== 'docket') return
    const side = change[changeSide].locations.length ? changeSide : change.new.locations.length ? 'new' : 'old'
    setSourceVersion(change[side].version)
    setSourcePage(change[side].page_start || 1)
  }, [change, changeId, changeSide, view])

  useEffect(() => {
    if (!projectId || view !== 'docket' || !workflow?.visible_version) return
    let live = true
    setPageData(null)
    api<SourcePage>(`/api/projects/${projectId}/sources/${sourceVersion}/pages/${sourcePage}`)
      .then(data => { if (live) setPageData(data) })
      .catch(err => { if (live) setError(err.message) })
    return () => { live = false }
  }, [projectId, view, sourceVersion, sourcePage, workflow?.visible_version])

  useEffect(() => {
    if (view !== 'docket' || !change || change.change_id !== changeId) return
    const frame = window.requestAnimationFrame(() => document.getElementById('source-page')?.scrollIntoView({ block: 'start' }))
    return () => window.cancelAnimationFrame(frame)
  }, [view, change, changeId])

  useEffect(() => {
    if (view !== 'docket' || !change || change.change_id !== changeId || !pageData) return
    const side = change.old.version === sourceVersion ? 'old' : 'new'
    if (pageData.version !== sourceVersion || pageData.page !== sourcePage || sourcePage !== change[side].page_start) return
    const frame = window.requestAnimationFrame(() => document.getElementById('cited-change-line')?.scrollIntoView({ block: 'center' }))
    return () => window.cancelAnimationFrame(frame)
  }, [view, change, changeId, pageData, sourceVersion, sourcePage])

  useEffect(() => {
    const onPop = () => {
      const next = new URLSearchParams(window.location.search)
      setProjectId(next.get('project') || projects[0]?.id || '')
      setView(next.get('view') === 'docket' ? 'docket' : 'artifacts')
      setChangeId(next.get('change') || '')
      setChangeSide(next.get('side') === 'old' ? 'old' : 'new')
      const pairRun = workflow?.runs.find(run => run.comparison?.old === next.get('old') && run.comparison?.new === next.get('new'))
      if (pairRun) setSelectedRunId(pairRun.run_id)
      setFindingId('')
      setResetOpen(false)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [projects, workflow])

  useEffect(() => {
    if (!findingId) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setFindingId('') }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [findingId])

  useEffect(() => {
    if (!resetOpen) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setResetOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [resetOpen])

  function navigate(nextView: View, nextChange = '', nextProject = projectId, nextSide: ChangeSide = 'new') {
    window.history.pushState({}, '', urlFor(nextProject, nextView, nextChange, nextSide, nextChange ? activeComparison : null))
    setProjectId(nextProject); setView(nextView); setChangeId(nextChange)
    setChangeSide(nextSide)
    setFindingId(''); setResetOpen(false); setUploadOpen(false)
  }

  function selectSourceVersion(version: string) {
    setSourceVersion(version)
    if (change && change.old.version !== version && change.new.version !== version) {
      setChangeId(''); setChange(null); setSourcePage(1)
      window.history.replaceState({}, '', urlFor(projectId, view))
      return
    }
    const side: ChangeSide = change?.old.version === version ? 'old' : 'new'
    setChangeSide(side)
    setSourcePage(change?.[side].page_start || 1)
    if (changeId) window.history.replaceState({}, '', urlFor(projectId, view, changeId, side, activeComparison))
  }

  async function introduce() {
    if (!projectId || busy) return
    setBusy(true); setError('')
    try {
      const state = await api<Workflow>(`/api/projects/${projectId}/introduce-change`, { method: 'POST' })
      setWorkflow(state)
      setSelectedRunId(state.runs.at(-1)?.run_id || null)
      setSourceVersion(state.visible_version || 'v1'); setSourcePage(1); setPageData(null)
      navigate('artifacts')
      setArtifact('obligations')
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not introduce the filing') }
    finally { setBusy(false) }
  }

  async function uploadDocument(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!projectId || !uploadFile || (uploadMode === 'revision' && !uploadRevises) || busy) return
    setBusy(true); setError('')
    try {
      const query = new URLSearchParams({ title: uploadTitle.trim(), original_filename: uploadFile.name, issued_on: uploadDate, status: uploadStatus })
      if (uploadMode === 'revision') query.set('revises', uploadRevises)
      const added = await api<{ version: string; workflow: Workflow }>(`/api/projects/${projectId}/filings?${query}`, {
        method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: uploadFile,
      })
      const detail = await api<ProjectDetail>(`/api/projects/${projectId}`)
      setProject(detail); setWorkflow(added.workflow)
      setSourceVersion(added.version); setSourcePage(1); setPageData(null)
      setUploadOpen(false); setUploadFile(null)
      setChangeId(''); setChange(null); setFindingId('')
      if (uploadMode === 'revision') {
        setSelectedRunId(added.workflow.runs.at(-1)?.run_id || null)
        navigate('artifacts')
      } else {
        navigate('docket')
      }
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not add the PDF') }
    finally { setBusy(false) }
  }

  async function createObligation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!projectId || busy) return
    setBusy(true); setError('')
    try {
      const added = await api<{ obligation: Obligation; workflow: Workflow }>(`/api/projects/${projectId}/obligations`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: obligationTitle.trim(), text: obligationText.trim() }),
      })
      const detail = await api<ProjectDetail>(`/api/projects/${projectId}`)
      setProject(detail); setWorkflow(added.workflow)
      setSelectedRunId(added.workflow.runs.at(-1)?.run_id || null)
      setObligationOpen(false); setObligationTitle(''); setObligationText('')
      setArtifact('obligations')
      setNewObligationId(added.obligation.id)
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not add the obligation') }
    finally { setBusy(false) }
  }

  async function resetDemo() {
    if (!projectId || busy) return
    setResetOpen(false)
    setBusy(true); setError('')
    try {
      const state = await api<Workflow>(`/api/projects/${projectId}/reset-demo`, { method: 'POST' })
      setWorkflow(state)
      setSelectedRunId(null); setFindingId(''); setChangeId(''); setChange(null)
      setSourceVersion(state.visible_version || 'v1'); setSourcePage(1); setPageData(null)
      window.history.replaceState({}, '', urlFor(projectId, 'docket'))
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not reset the demo') }
    finally { setBusy(false) }
  }

  async function recordDecision(action: DecisionAction) {
    if (!projectId || !selectedRun || !findingId || decisionBusy) return
    setDecisionBusy(true); setError('')
    try {
      const state = await api<Workflow>(`/api/projects/${projectId}/decisions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: selectedRun.run_id, obligation_id: findingId, action }),
      })
      setWorkflow(state)
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not record the decision') }
    finally { setDecisionBusy(false) }
  }

  const docket = project?.docket_details[0]
  const visibleVersions = workflow?.visible_versions || []
  const nextPrepared = docket?.comparisons.find(item => !item.legacy_direct && !item.manual_upload && !item.backfill && visibleVersions.includes(item.old) && !visibleVersions.includes(item.new))
  const canIntroduce = Boolean(nextPrepared && ['idle', 'completed'].includes(workflow?.review.status || ''))
  const hasUploads = Boolean(docket && Object.values(docket.versions).some(source => source.uploaded))
  const selectedReview = selectedRun?.review || workflow?.review
  const activeResults = selectedRun?.results || workflow?.results || []
  const selectedResult = activeResults.find(item => item.obligation_id === findingId)
  const selectedDecisionHistory = workflow && selectedRun ? decisionsFor(workflow, selectedRun.run_id, findingId) : []
  const latestFlaggedCount = (workflow?.results || []).filter(item => item.status === 'complete' && item.assessment && item.assessment.outcome !== 'not_affected').length
  const latestReviewMessage = workflow?.review.status === 'reviewing'
    ? `Review in progress · ${workflow.review.completed} of ${workflow.review.total} ${workflow.review.total === 1 ? 'obligation' : 'obligations'}`
    : workflow?.review.status === 'completed'
      ? `Review complete · ${latestFlaggedCount} ${latestFlaggedCount === 1 ? 'obligation' : 'obligations'} flagged`
      : workflow?.review.error || 'Review ready'
  const currentSource = docket && workflow?.visible_version ? docket.versions[workflow.visible_version] : null
  const selectedSource = docket?.versions[sourceVersion]
  const uncomparedFiling = Boolean(selectedSource?.uploaded && !selectedSource.revises)

  function showGuide(target: EventTarget | null) {
    if (!(target instanceof Element)) return
    const trigger = target.closest<HTMLElement>('[data-guide]')
    if (!trigger) return
    const text = trigger.dataset.guide
    if (!text) return
    const rect = trigger.getBoundingClientRect()
    const width = Math.min(280, window.innerWidth - 32)
    const above = rect.bottom + 110 > window.innerHeight && rect.top > 110
    setGuide({ text, left: Math.max(16, Math.min(rect.left, window.innerWidth - width - 16)), top: above ? rect.top - 8 : rect.bottom + 8, above })
  }

  function hideGuide(event: MouseEvent<HTMLDivElement> | FocusEvent<HTMLDivElement>) {
    const current = event.target instanceof Element ? event.target.closest('[data-guide]') : null
    if (current && event.relatedTarget instanceof Node && current.contains(event.relatedTarget)) return
    setGuide(null)
  }

  useEffect(() => {
    const hide = () => setGuide(null)
    window.addEventListener('scroll', hide, true)
    window.addEventListener('resize', hide)
    return () => { window.removeEventListener('scroll', hide, true); window.removeEventListener('resize', hide) }
  }, [])

  return <div className="app" onMouseOver={event => showGuide(event.target)} onMouseOut={hideGuide} onFocusCapture={event => showGuide(event.target)} onBlurCapture={hideGuide} onClickCapture={() => setGuide(null)}>
    <header className="global-header">
      <button className="wordmark" onClick={() => navigate('artifacts')} aria-label="Strata home"><span className="logo-bars"><i /><i /><i /></span>strata</button>
      <span className="header-divider" />
      <label className="project-switch"><span>Project</span><select aria-label="Choose project" data-guide="Switch between example projects. Each project has its own docket and review history." aria-description="Switch between example projects. Each project has its own docket and review history." value={projectId} onChange={event => navigate('artifacts', '', event.target.value)}>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <span className="header-spacer" />
      <span className="local-label">Local workspace</span>
    </header>

    {error && <div className="error-message" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError('')}>Close</button></div>}
    {!project || !workflow ? <main className="loading-state">{error ? 'The project could not be loaded.' : 'Opening project…'}</main> : <>
      <div className="project-header"><div className="project-title"><span className="organization">{project.organization}</span><h1>{project.name}</h1></div><div className="project-tabs" role="navigation" aria-label="Project sections"><button className={view === 'artifacts' ? 'active' : ''} data-guide="View the project's obligation list, project record, and review history." aria-description="View the project's obligation list, project record, and review history." onClick={() => navigate('artifacts')}>Project artifacts</button><button className={view === 'docket' ? 'active' : ''} data-guide="Add new versions of regulatory filings here. Start with the next prepared version to run the demo review." aria-description="Add new versions of regulatory filings here. Start with the next prepared version to run the demo review." onClick={() => navigate('docket')}>Docket</button></div></div>

      {view === 'artifacts' ? <div className={'artifact-layout' + (docket && artifact === 'obligations' ? ' has-review-rail' : '')}>
        <aside className="file-nav"><h2>Project artifacts</h2><button className={artifact === 'obligations' ? 'active' : ''} data-guide="This is the demo's compiled list of obligations. In a full workflow, obligations would be extracted from project artifacts." aria-description="This is the demo's compiled list of obligations. In a full workflow, obligations would be extracted from project artifacts." onClick={() => setArtifact('obligations')}><span className="file-glyph">≡</span><span>{project.obligations_file || 'obligations.md'}<small>Obligation document · {workflow.obligation_version}</small></span></button><button className={artifact === 'project' ? 'active' : ''} data-guide="View project details and its linked docket." aria-description="View project details and its linked docket." onClick={() => setArtifact('project')}><span className="file-glyph">{`{}`}</span><span>project.json<small>Project record</small></span></button>{docket && <div className="linked-docket"><span>Linked docket</span><button data-guide="Open the regulatory filings tracked for this project." aria-description="Open the regulatory filings tracked for this project." onClick={() => navigate('docket')}>{docket.docket_id}<small>{workflow.visible_version} · {currentSource?.status.replace('_', ' ')}</small></button></div>}</aside>
        <main className="artifact-main">
          {artifact === 'obligations' ? <>
            <div className="document-topline"><div><strong>{project.obligations_file || 'obligations.md'}</strong><span>Version {workflow.obligation_version}</span>{workflow.runs.length > 1 && selectedRun?.review.started_at && <span>Review {new Date(selectedRun.review.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>}</div><button className="document-add-action" data-guide="Append a new obligation and review it against the current filing history when a revision exists." aria-description="Append a new obligation and review it against the current filing history when a revision exists." onClick={() => setObligationOpen(true)} disabled={busy || workflow.review.status === 'reviewing'}>Add obligation</button></div>
            {workflow.runs.length > 0 && <div className={'review-notice workflow-notice ' + (workflow.review.status === 'reviewing' ? 'progress' : workflow.review.status === 'completed' ? 'complete' : 'failed')} role="status" aria-atomic="true"><span className="review-pulse" aria-hidden="true" /><div><strong>{latestReviewMessage}</strong>{workflow.review.status === 'reviewing' && <span>Checking against the filing history. Findings appear here as they complete.</span>}{workflow.review.status === 'completed' && <span>Open an obligation's Review findings to inspect its draft assessment and source passages.</span>}{!['reviewing', 'completed'].includes(workflow.review.status) && <span>See Reviews for the saved result or retry action.</span>}</div></div>}
            {!docket && <div className="review-notice quiet"><span className="notice-icon">i</span><span>No docket is linked to this project.</span></div>}
            {selectedRun && (selectedRun.obligation_version || 'v1') !== workflow.obligation_version && <div className="review-notice quiet"><span className="notice-icon">i</span><span>This review used obligation document {selectedRun.obligation_version || 'v1'}. Select a newer review to see later obligations.</span></div>}
            {currentSource?.uploaded && !currentSource.revises && <div className="review-notice quiet"><span className="notice-icon">i</span><span>The latest filing has no comparison or impact review yet. Any findings below belong to an earlier review.</span></div>}
            <article className="document-page"><div className="document-masthead"><span>{project.organization}</span><span>PROJECT ARTIFACT · {workflow.obligation_version.toUpperCase()}</span></div><h2>Obligations</h2><p className="document-description">{project.name}</p><div className="document-rule" />
              {project.obligations.map((item, index) => {
                const parsed = parseObligation(item.text)
                const result = activeResults.find(entry => entry.obligation_id === item.id)
                const finding = result?.status === 'complete' && result.assessment?.outcome !== 'not_affected'
                const decision = selectedRun && decisionsFor(workflow, selectedRun.run_id, item.id).at(-1)
                return <section className="obligation-section" key={item.id} id={item.id}><div className="obligation-heading"><span className="section-number">{String(index + 1).padStart(2, '0')}</span><div><span className="obligation-id">{item.id}</span><h3>{item.title}</h3></div><div className="obligation-marker">{finding ? <><span className={'decision-state ' + (decision?.action || 'draft')}>{decision ? decisionLabel[decision.action] : 'Draft'}</span><button data-guide="Open this draft review to inspect its finding, proposed action, and source citations." aria-description="Open this draft review to inspect its finding, proposed action, and source citations." onClick={() => setFindingId(item.id)} aria-label={`Review findings for ${item.id}`}><span className="marker-dot" /> Review findings <span aria-hidden="true">&gt;</span></button></> : result?.status === 'failed' ? <span className="section-state failed">Review failed</span> : result?.status === 'complete' ? <span className="section-state">No update flagged</span> : selectedReview?.status === 'reviewing' ? <span className="section-state">{index < selectedReview.completed ? 'Reviewed' : index === selectedReview.completed ? 'Reviewing' : 'Queued'}</span> : selectedRun ? <span className="section-state">Not reviewed in this run</span> : null}</div></div><div className="obligation-body">{parsed.body.map((paragraph, paragraphIndex) => <p key={paragraphIndex}>{paragraph}</p>)}</div><div className="obligation-metadata">{parsed.metadata.map((line, lineIndex) => <span key={lineIndex}>{line}</span>)}</div></section>
              })}
            </article>
          </> : <><div className="document-topline"><div><strong>project.json</strong><span>Project record</span></div><span>View only</span></div><article className="document-page project-record"><div className="document-masthead"><span>{project.organization}</span><span>PROJECT ARTIFACT</span></div><h2>Project record</h2><dl><dt>Name</dt><dd>{project.name}</dd><dt>Organization</dt><dd>{project.organization}</dd><dt>Project ID</dt><dd>{project.id}</dd><dt>Linked dockets</dt><dd>{project.dockets.length ? project.dockets.join(', ') : 'None'}</dd></dl></article></>}
        </main>
        {docket && artifact === 'obligations' && <ReviewRail workflow={workflow} docket={docket} selectedRun={selectedRun} onSelect={run => { setSelectedRunId(run.run_id); setFindingId(''); setChange(null) }} onRetry={introduce} onDocket={() => navigate('docket')} busy={busy} />}
      </div> : <main className="docket-layout">
        {!docket ? <div className="docket-empty"><h2>No docket linked to this project</h2><p>The project record does not name a docket.</p></div> : <div className="docket-workspace">
          <aside className="file-nav docket-files" aria-label="Docket files"><h2>Docket files</h2>
            {visibleVersions.map(version => <button key={version} className={sourceVersion === version ? 'active' : ''} data-guide="Read this PDF's extracted pages. Its filing date and source status appear below its original filename." aria-description="Read this PDF's extracted pages. Its filing date and source status appear below its original filename." onClick={() => selectSourceVersion(version)}><span className="file-glyph pdf-glyph">PDF</span><span>{revisionLabel(docket, visibleVersions, version) && `${revisionLabel(docket, visibleVersions, version)} · `}{sourceLabel(docket.versions[version])}<small>{docket.versions[version].title && `${docket.versions[version].title} · `}{statusLabel(docket.versions[version])} · {stamp(docket.versions[version].date)}</small></span></button>)}
            {canIntroduce && <div className="docket-file-action"><button className="primary-action" data-guide="Adds the next prepared filing and starts the change review workflow." aria-description="Adds the next prepared filing and starts the change review workflow." onClick={introduce} disabled={busy}>{busy ? 'Introducing…' : 'Introduce new version'}</button></div>}
            <div className="docket-file-action"><button className="secondary-action" data-guide="Add a new filing or a revision of an existing filing." aria-description="Add a new filing or a revision of an existing filing." onClick={() => { setUploadMode('new'); setUploadRevises(sourceVersion); setUploadOpen(true) }} disabled={busy || workflow.review.status === 'reviewing'}>Add PDF</button></div>
          </aside>
          <div className="docket-main">
            <div className="docket-header"><div><span className="kicker">{docket.agency} / {docket.docket_id}</span><h2>{docket.title}</h2></div><div className="docket-header-actions"><span className="docket-version-count">{visibleVersions.length} {visibleVersions.length === 1 ? 'document' : 'documents'}</span>{workflow.runs.length > 0 && !hasUploads && <button className="reset-action" data-guide="Reset this project to v1 so the demo can be replayed. Saved reviews and decisions are deleted; prepared PDFs and obligations remain." aria-description="Reset this project to v1 so the demo can be replayed. Saved reviews and decisions are deleted; prepared PDFs and obligations remain." onClick={() => setResetOpen(true)} disabled={busy}>Reset to v1</button>}</div></div>
            {hasUploads && docket.comparisons.length > 0 && <p className="docket-upload-note">Demo reset is unavailable after a PDF upload; added filings and reviews are preserved.</p>}
            {visibleVersions.length === 0 && <div className="docket-review-status"><span className="status-indicator" />This docket has no PDFs yet. Use Add PDF to upload its first filing.</div>}
            {uncomparedFiling ? <div className="docket-review-status"><span className="status-indicator" />New filing added. Add a revision of this filing to compare and review it.</div> : workflow.runs.length > 0 && <div className="docket-review-status"><span className={`status-indicator ${workflow.review.status}`} />{latestReviewMessage}<button data-guide="Return to the obligations and inspect the review's flagged records." aria-description="Return to the obligations and inspect the review's flagged records." onClick={() => navigate('artifacts')}>Open obligations →</button></div>}
            {selectedSource && <SourceReader
              projectId={projectId} docketId={docket.docket_id} sourceLabel={sourceLabel(docket.versions[sourceVersion])} revisionName={revisionLabel(docket, visibleVersions, sourceVersion)}
              version={sourceVersion} page={sourcePage}
              pageData={pageData} change={change?.change_id === changeId ? change : null}
              onPage={setSourcePage} onClearChange={() => navigate('docket')}
            />}
          </div>
        </div>}
      </main>}
      {findingId && selectedResult?.assessment && <FindingPanel projectId={projectId} comparison={activeComparison} sourceStatus={activeComparison && docket?.versions[activeComparison.new]?.status} result={selectedResult} decisionHistory={selectedDecisionHistory} canDecide={selectedRun === workflow.runs.at(-1)} deciding={decisionBusy} onDecision={recordDecision} onClose={() => setFindingId('')} onChange={(id, side) => navigate('docket', id, projectId, side)} />}
      {resetOpen && <div className="reset-overlay"><div className="reset-dialog" role="alertdialog" aria-modal="true" aria-labelledby="reset-title" aria-describedby="reset-description"><h2 id="reset-title">Reset project to v1?</h2><p id="reset-description">Saved reviews and decisions will be deleted. Later filings and their changes will be hidden until you introduce them again. Prepared PDFs and obligations remain.</p><div className="reset-dialog-actions"><button onClick={() => setResetOpen(false)} autoFocus>Cancel</button><button className="reset-confirm" onClick={resetDemo}>Reset to v1</button></div></div></div>}
      {uploadOpen && docket && <div className="reset-overlay"><form className="reset-dialog upload-dialog" onSubmit={uploadDocument} aria-label="Add PDF to docket">
        <h2>Add PDF to docket</h2>
        <p>Start a separate filing or add a revision to one already in this docket.</p>
        <label>Relationship<select value={uploadMode} onChange={event => setUploadMode(event.target.value as 'new' | 'revision')}><option value="new">New filing</option>{visibleVersions.length > 0 && <option value="revision">Revision of existing filing</option>}</select></label>
        <label>PDF file<input type="file" accept="application/pdf,.pdf" required onChange={event => { const file = event.target.files?.[0] || null; setUploadFile(file); if (file && !uploadTitle) setUploadTitle(file.name.replace(/\.pdf$/i, '')) }} /></label>
        <label>Title<input value={uploadTitle} required maxLength={160} onChange={event => setUploadTitle(event.target.value)} /></label>
        <label>Filing date<input type="date" value={uploadDate} required onChange={event => setUploadDate(event.target.value)} /></label>
        <label>Source status<select value={uploadStatus} onChange={event => setUploadStatus(event.target.value)}><option value="draft_proposal">Draft proposal</option><option value="proposed">Proposed</option><option value="final">Final</option></select></label>
        {uploadMode === 'revision' && <label>Revises<select value={uploadRevises} required onChange={event => setUploadRevises(event.target.value)}>{visibleVersions.map(version => <option key={version} value={version}>{revisionLabel(docket, visibleVersions, version) && `${revisionLabel(docket, visibleVersions, version)} · `}{sourceLabel(docket.versions[version])} · {statusLabel(docket.versions[version])}</option>)}</select></label>}
        <div className="reset-dialog-actions"><button type="button" onClick={() => setUploadOpen(false)} disabled={busy}>Cancel</button><button className="reset-confirm" type="submit" disabled={busy || !uploadFile}>{busy ? uploadMode === 'revision' ? 'Comparing PDF…' : 'Adding PDF…' : uploadMode === 'revision' ? 'Add and review' : 'Add filing'}</button></div>
      </form></div>}
      {obligationOpen && <div className="reset-overlay"><form className="reset-dialog upload-dialog" role="dialog" aria-modal="true" aria-label="Add obligation" onSubmit={createObligation}>
        <h2>Add obligation</h2>
        <p>Append a company obligation to this project. If the current filing has revisions, its history will be reviewed now. Otherwise review starts when a revision is added.</p>
        <label>Title<input value={obligationTitle} required maxLength={160} autoFocus onChange={event => setObligationTitle(event.target.value)} /></label>
        <label>Obligation text<textarea value={obligationText} required maxLength={20000} rows={8} onChange={event => setObligationText(event.target.value)} /></label>
        <div className="reset-dialog-actions"><button type="button" onClick={() => setObligationOpen(false)} disabled={busy}>Cancel</button><button className="reset-confirm" type="submit" disabled={busy || !obligationTitle.trim() || !obligationText.trim()}>{busy ? 'Adding…' : 'Add obligation'}</button></div>
      </form></div>}
    </>}
    {guide && createPortal(<div className={'guide-popover' + (guide.above ? ' above' : '')} role="tooltip" style={{ left: guide.left, top: guide.top }}>{guide.text}</div>, document.body)}
  </div>
}

function ReviewRail({ workflow, docket, selectedRun, onSelect, onRetry, onDocket, busy }: {
  workflow: Workflow
  docket: Docket
  selectedRun: ReviewRun | undefined
  onSelect: (run: ReviewRun) => void
  onRetry: () => void
  onDocket: () => void
  busy: boolean
}) {
  const runs = [...workflow.runs].reverse()
  const latest = workflow.runs.at(-1)
  return <aside className="review-rail" aria-label="Review history">
    <div className="review-rail-heading" data-guide="Select a review to see its saved findings for that version comparison." aria-description="Select a review to see its saved findings for that version comparison."><h2>Reviews</h2><span>{runs.length}</span></div>
    {runs.length ? <ol className="review-timeline">{runs.map((run, index) => {
      const flaggedObligations = run.results.filter(item => item.status === 'complete' && item.assessment && item.assessment.outcome !== 'not_affected').length
      const reason = run.trigger === 'retry' ? 'Review retried' : run.trigger === 'obligation_added' ? 'Obligation added' : run.trigger === 'manual_upload' ? 'PDF revision added' : run.comparison ? 'Changes detected between versions' : 'Source review'
      const started = run.review.started_at ? new Date(run.review.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'Time unavailable'
      const status = run.review.status === 'reviewing' ? `${run.review.completed}/${run.review.total} reviewed` : run.review.status.replace('_', ' ')
      return <li key={run.run_id || index} className={selectedRun === run ? 'selected' : ''}>
        <button className="review-run" data-guide="Show this run's saved findings and cited changes on the obligations document." aria-description="Show this run's saved findings and cited changes on the obligations document." onClick={() => onSelect(run)} aria-current={selectedRun === run ? 'true' : undefined}>
          <span className="review-run-meta"><time>{started}</time><span className={`run-status ${run.review.status}`}>{status}</span></span>
          <strong>{reason}</strong>
          {run.comparison && <span className="review-run-source">{run.comparison.docket_id} · {displayVersion(docket, workflow.visible_versions, run.comparison.old)} → {displayVersion(docket, workflow.visible_versions, run.comparison.new)}</span>}
          <span className="review-run-findings">{flaggedObligations} {flaggedObligations === 1 ? 'obligation' : 'obligations'} flagged</span>
        </button>
        {run === latest && run.review.status === 'reviewing' && <div className="rail-progress"><i style={{ width: `${run.review.total ? run.review.completed / run.review.total * 100 : 0}%` }} /></div>}
        {run === latest && ['failed', 'partial', 'interrupted'].includes(run.review.status) && <div className="rail-retry"><span>{run.review.error || 'Review needs attention.'}</span><button onClick={onRetry} disabled={busy}>{busy ? 'Retrying…' : 'Retry review'}</button></div>}
      </li>
    })}</ol> : <div className="review-rail-empty"><p>No reviews yet. Open the docket and introduce the next filing to start a review.</p><button onClick={onDocket}>Open docket →</button></div>}
  </aside>
}

function FindingPanel({ projectId, comparison, sourceStatus, result, decisionHistory, canDecide, deciding, onDecision, onClose, onChange }: {
  projectId: string
  comparison?: { old: string; new: string } | null
  sourceStatus?: string | null
  result: Result
  decisionHistory: Decision[]
  canDecide: boolean
  deciding: boolean
  onDecision: (action: DecisionAction) => void
  onClose: () => void
  onChange: (id: string, side: ChangeSide) => void
}) {
  const assessment = result.assessment!
  const preliminary = sourceStatus !== 'final'
  const legacyPreliminary = preliminary && assessment.review_policy_version !== 1
  const actions = legacyPreliminary
    ? { intro: '', items: ['Review the cited proposal and consider its possible impact. Keep the current obligation unchanged until the final source and company applicability are verified.'] }
    : proposedActionParts(assessment.proposed_action)
  const decision = decisionHistory.at(-1)
  const citations = Array.from(new Set(assessment.citations.map(citation => citation.change_id))).map(id => ({
    id,
    passages: assessment.citations.filter(citation => citation.change_id === id),
  }))
  return <>
    <button className="panel-scrim" aria-label="Close review" onClick={onClose} />
    <aside className="finding-panel" role="dialog" aria-modal="true" aria-label={`Review for ${result.obligation_id}`}>
      <div className="finding-header"><div><span className="kicker">{decision ? 'MANAGER DECISION' : 'DRAFT REVIEW'}</span><h2>{result.obligation_id} review</h2></div><button className="close-button" onClick={onClose} aria-label="Close review">×</button></div>
      <div className="finding-scroll">
        <span className={`outcome-label ${assessment.outcome}`}>{preliminary && assessment.outcome === 'affected' ? 'possible impact' : assessment.outcome.replace('_', ' ')}</span>
        {preliminary && <p className="source-stage-note">{sourceStatus === 'draft_proposal' ? 'Draft proposal' : 'Proposed source'} · Consider possible impact. This source does not support changing the obligation yet.</p>}
        {sourceStatus === 'final' && assessment.outcome === 'needs_review' && <p className="source-stage-note">Final source · The review has not established that this obligation needs an update. Verify the cited text and company applicability first.</p>}
        {legacyPreliminary && <p className="source-stage-note legacy">This saved assessment predates the proposal-stage check. Its earlier finding and action wording are withheld; inspect the citations and confirm the final source before editing records.</p>}
        <section className="finding-section source-links"><h3>Citation review <span className="citation-count">{citations.length}</span></h3><p className="section-helper">Select a cited change to inspect its {comparison?.old || 'old'} → {comparison?.new || 'new'} diff in the docket.</p>
          {citations.length ? <div className="change-rail">{citations.map(({ id, passages }, index) => {
            const side = passages.some(citation => citation.side === 'new') ? 'new' : 'old'
            return <a key={id} href={urlFor(projectId, 'docket', id, side, comparison)} data-guide="Open the cited source page with changed lines highlighted. Compare it with the original PDF before acting." aria-description="Open the cited source page with changed lines highlighted. Compare it with the original PDF before acting." onClick={event => { event.preventDefault(); onChange(id, side) }} aria-label={`Review source diff for ${id}`}><span className="rail-node">{index + 1}</span><span><strong>{id}</strong><small>{passages.map(citation => `${citation.version} ${citation.status.replace('_', ' ')} · p. ${citation.pdf_page_start ?? '—'}`).join(' / ')}</small><span className="rail-quote">{compact(passages.find(citation => citation.side === 'new')?.quote || passages[0].quote, 120)}</span><span className="rail-action">Review source diff →</span></span></a>
          })}</div> : <p className="citation-empty">No source citation was provided for this draft.</p>}
        </section>
        <section className="finding-section"><h3>Finding</h3><p>{legacyPreliminary ? 'This proposed-stage change may relate to the obligation. The final text and company applicability still need review.' : assessment.summary}</p></section>
        {!legacyPreliminary && <section className="finding-section reasoning-section"><details><summary>View reasoning</summary><p>{assessment.rationale}</p></details></section>}
        <section className="finding-section"><h3>{preliminary ? 'For consideration' : assessment.outcome === 'affected' ? 'Recommended update' : 'Next steps'}</h3>{actions.intro && <p className="action-intro">{actions.intro}</p>}<ul className="action-list">{actions.items.map((item, index) => <li key={index}>{item}</li>)}</ul><div className="reviewer-role">Reviewer: {assessment.reviewer_role}</div></section>
        {assessment.open_questions.length > 0 && <section className="finding-section"><h3>Open questions</h3>{assessment.open_questions.map((question, index) => <p key={index}>{question}</p>)}</section>}
        <section className="finding-section decision-gate"><h3>Manager decision</h3>
          {decision && <p className={'decision-record ' + decision.action}>{decisionLabel[decision.action]} · {new Date(decision.decided_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</p>}
          {canDecide ? <><p className="decision-helper">Record a decision on this draft assessment. The obligation text is not edited.</p><div className="decision-actions">
            <button className="accept" data-guide="Record local acceptance of this draft assessment. The obligation text is not changed." aria-description="Record local acceptance of this draft assessment. The obligation text is not changed." aria-pressed={decision?.action === 'accept'} onClick={() => onDecision('accept')} disabled={deciding}>{preliminary ? 'Mark for consideration' : 'Accept recommendation'}</button>
            <button data-guide="Record local rejection of this draft proposal." aria-description="Record local rejection of this draft proposal." aria-pressed={decision?.action === 'reject'} onClick={() => onDecision('reject')} disabled={deciding}>Reject</button>
            <button data-guide="Mark this proposal for Legal review locally. No notification is sent." aria-description="Mark this proposal for Legal review locally. No notification is sent." aria-pressed={decision?.action === 'route_to_legal'} onClick={() => onDecision('route_to_legal')} disabled={deciding}>Route to Legal</button>
          </div>{decision?.action === 'route_to_legal' && <p className="decision-helper">Marked for Legal review; no notification was sent.</p>}</> : <p className="decision-helper">Earlier review. Decisions can be recorded on the latest review.</p>}
          {decisionHistory.length > 1 && <details className="decision-history"><summary>Decision history</summary><ol>{[...decisionHistory].reverse().map(item => <li key={item.id}>{decisionLabel[item.action]} · {new Date(item.decided_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</li>)}</ol></details>}
        </section>
      </div>
      <div className="finding-footer">{decision ? 'Decision recorded locally' : preliminary ? 'Proposal-stage assessment · wait for final evidence before changing records' : 'Draft assessment · verify against the cited source before acting'}</div>
    </aside>
  </>
}

function SourceReader({ projectId, docketId, sourceLabel, revisionName, version, page, pageData, change, onPage, onClearChange }: {
  projectId: string
  docketId: string
  sourceLabel: string
  revisionName: string | null
  version: string
  page: number
  pageData: SourcePage | null
  change: ChangeDetail | null
  onPage: (page: number) => void
  onClearChange: () => void
}) {
  const visiblePage = pageData?.version === version && pageData.page === page ? pageData : null
  const side: ChangeSide | null = change ? change.old.version === version ? 'old' : 'new' : null
  const positions = side ? change![side].locations.filter(location => location.page === page).map(location => location.line) : []
  const highlighted = new Set(positions)
  const firstLine = positions.length ? Math.min(...positions) : null
  const lastLine = positions.length ? Math.max(...positions) : null
  const counterpart = change?.diff_lines.filter(line => line.type === (side === 'new' ? 'removed' : 'added')) || []
  const targetPage = side ? change?.[side].page_start : null
  const changedRows = (kind: 'added' | 'removed') => counterpart.map((line, index) =>
    <div className={'source-page-line ' + kind + ' counterpart'} key={'counterpart-' + index}>
      <span className="source-line-number">{kind === 'added' ? '+' : '−'}</span>
      <span>{line.text.slice(1)}</span>
    </div>
  )
  return <section className="source-view" id="source-page">
    <div className="source-toolbar">
      <div><span className="toolbar-overline">DOCKET DOCUMENT</span><strong>{revisionName && `${revisionName} · `}{sourceLabel}</strong></div>
    </div>
    <div className="extracted-page">
      <div className="page-controls">
        <span>Extracted page {page}{visiblePage ? ' of ' + visiblePage.page_count : ''}{change && <strong className="page-change-chip">{change.change_id}</strong>}</span>
        <div>
          <button disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button>
          <button disabled={!visiblePage || page >= visiblePage.page_count} onClick={() => onPage(page + 1)}>Next</button>
          <a href={'/api/projects/' + projectId + '/sources/' + version + '#page=' + page} data-guide="Open the source PDF to verify the extracted text and page location." aria-description="Open the source PDF to verify the extracted text and page location." target="_blank" rel="noreferrer">Open original PDF ↗</a>
        </div>
      </div>
      {change && <div className="change-page-note">
        <div><span className="kicker">CHANGE IN DOCUMENT</span><strong>{change.change_id}</strong><small>{highlighted.size ? 'Changed lines are highlighted in the full page below.' : targetPage ? 'This change is on page ' + targetPage + ' of this version.' : 'No location is recorded for this version.'}</small></div>
        <div className="change-page-actions">
          {targetPage && page !== targetPage && <button onClick={() => onPage(targetPage)}>Go to change</button>}
          <button onClick={onClearChange}>Close change</button>
        </div>
      </div>}
      <article className="pdf-text-page source-code-page">
        <div className="pdf-page-meta">{docketId} · {revisionName && `${revisionName} · `}{sourceLabel} · PDF PAGE {page}</div>
        {visiblePage ? visiblePage.lines.length ? visiblePage.lines.map((line, index) => {
          const lineNumber = index + 1
          const changed = highlighted.has(lineNumber)
          return <div key={index}>
            {side === 'new' && firstLine === lineNumber && changedRows('removed')}
            <div className={'source-page-line' + (changed ? ' ' + (side === 'new' ? 'added' : 'removed') : '')} id={changed && firstLine === lineNumber ? 'cited-change-line' : undefined}>
              <span className="source-line-number">{changed ? (side === 'new' ? '+' : '−') : ''}{lineNumber}</span>
              <span>{line}</span>
            </div>
            {side === 'old' && lastLine === lineNumber && changedRows('added')}
          </div>
        }) : <p>This page has no extractable text. Open the original PDF to inspect it.</p> : <p>Loading page…</p>}
        {change?.diff_truncated && highlighted.size > 0 && <p className="diff-truncation">Change preview is truncated. Open the original PDF for the complete source.</p>}
      </article>
    </div>
  </section>
}

export default App
