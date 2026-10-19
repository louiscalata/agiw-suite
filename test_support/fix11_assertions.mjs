import * as layout from '../web/map-layout.mjs';

// Exact observations supplied by the healthy fixture: totals, per-model identity,
// activity and slots, GPU, memory, callers, lane health, peer residency, route and
// job counts. Features without evidence (client sessions, AFM, Jev) are outside
// this contract and must continue to say unknown, even in a healthy runtime.
export function checkFix11({packet, api, $, input, check}) {
  const {snapshot, host} = packet, {dimension, expected: e} = snapshot.fixtureScenario;
  const tiles = $('activityTiles').textContent;
  if (dimension === 'healthy-exact') {
    check($('loadedCount').textContent === String(e.loaded), 'exact loaded', $('loadedCount').textContent);
    check($('activeCount').textContent === String(e.working), 'exact working', $('activeCount').textContent);
    check($('queuedCount').textContent === (host === 'mac' ? '0' : `${e.slotsBusy}/4`), 'exact queue or slots', $('queuedCount').textContent);
    check(tiles.includes(`Answered${e.answered}`) && !/Answered(?:Unknown|at least)/.test(tiles), 'exact answered', tiles);
    check(tiles.includes('Failed0'), 'exact failed', tiles);
    if (host === 'mac') {
      check(tiles.includes('In flight0') && tiles.includes('Routes0'), 'exact inactive routes/jobs', tiles);
      check($('activityStatus').textContent === 'Nothing running now', 'healthy live inactivity', $('activityStatus').textContent);
    }
    check(input.vitals.route.detail === 'Idle', 'exact route idle', input.vitals.route.detail);
    check(/GPU 0%/.test(input.vitals.mac.detail), 'exact GPU zero', input.vitals.mac.detail);
    check(!/unknown|unavailable|stale|at least/i.test(input.vitals.mac.detail), 'exact runtime chip', input.vitals.mac);
    const laneView = layout.windowsLaneView(snapshot.windowsWorker, null, {feedFresh:true});
    check(laneView.lanes.length === 2 && laneView.lanes.every(l => l.up === true && l.total === 2), 'exact lane health/capacity', laneView);
    const memory = layout.memoryView(snapshot.memory, {feedFresh:true, host});
    check(memory.known && memory.level === 'ok', 'exact memory', memory);
    check(/None connected/.test($('inspector').textContent), 'exact callers', $('inspector').textContent);
    for (const row of snapshot.models) {
      api.select(`model:${host}:${row.id}`); api.render();
      const text = $('inspector').textContent;
      check(text.includes(row.id), 'exact model identity/alias', text);
      check(!/Activity unknown|Signal stale|Not loaded/.test(text), 'exact model activity', text);
    }
    const report = layout.pcReportView(snapshot.windowsLive, {feedFresh:true});
    check(report.known && /1 loaded.*idle/i.test(report.words) && !/unknown|at least/i.test(report.words), 'exact peer report', report);
  }
  if (dimension === 'route-semantics') {
    const src = snapshot.sources.find(s => s.id === 'pipeline-router');
    check(src?.observationAgeSeconds === snapshot.fixtureScenario.inputs.observation, 'route observation clock', src);
    if (e.current) {
      check(snapshot.pipeline.status === e.state && snapshot.pipeline.runId === 'route', 'exact pipeline state/identity', snapshot.pipeline);
      check(e.state === 'running' ? /^Running/.test(input.vitals.route.detail) : /^Queued/.test(input.vitals.route.detail), 'exact route chip', input.vitals.route);
      check(input.feed.some(r => r.kind === 'route' && r.state === (e.state === 'running' ? 'in-flight' : 'queued')), 'exact route feed state', input.feed);
      check(!/last sample|unknown|at least/i.test($('activityStatus').textContent), 'exact current route summary', $('activityStatus').textContent);
      if (e.state === 'running' && e.eventAge === null) check(/duration unknown/.test($('activityMeaning').textContent) && !/\(0 s\)/.test($('activityMeaning').textContent), 'unknown route duration never zero', $('activityMeaning').textContent);
    } else {
      check(!/^Running|^Queued|^Idle/.test(input.vitals.route.detail), 'expired route chip', input.vitals.route);
      check(!input.feed.some(r => r.kind === 'route' && ['in-flight','queued'].includes(r.state)), 'expired route feed', input.feed);
      check(!/Nothing running now|In flight0/.test($('activityStatus').textContent+tiles), 'expired route inactivity', tiles);
    }
    if (e.current) check(snapshot.pipeline.pipelines[0]?.ageSeconds === e.eventAge
      && (e.state === 'queued' || snapshot.pipeline.ageSeconds === e.eventAge), 'retained checkpoint age', snapshot.pipeline);
  }
  if (dimension === 'journal-semantics') {
    const src = snapshot.sources.find(s => s.id === 'windows-jobs');
    check(src?.observationAgeSeconds === snapshot.fixtureScenario.inputs.observation, 'journal observation clock', src);
    if (e.current) {
      check(tiles.includes(`Answered${e.running ? 0 : 1}`) && !/Answered(?:Unknown|at least)/.test(tiles), 'exact journal count', tiles);
      check(tiles.includes(`In flight${e.running ? 1 : 0}`), 'exact journal liveness', tiles);
      check(e.running ? input.feed.some(r => r.state === 'in-flight') : /Nothing running now/.test($('activityStatus').textContent), 'exact journal summary', $('activityStatus').textContent);
      if (!e.running) check(api.nodeSubtitle('windows-worker').includes('LAST INFERENCE'), 'recent success visible', api.nodeSubtitle('windows-worker'));
    } else {
      check(/AnsweredUnknown/.test(tiles), 'expired journal coverage', tiles);
      check(!input.feed.some(r => r.kind === 'pc-job' && r.state === 'in-flight'), 'expired journal liveness', input.feed);
    }
  }
  if (dimension === 'frozen-capture') {
    const text = ['headline','subline','activityStatus','activityMeaning','activityTiles','activityList'].map(id => $(id).textContent).join(' ');
    check(!/\bnow\b/i.test(text), 'frozen never now', text);
    check(text.includes(new Date(e.capture * 1000).toISOString()), 'frozen capture time', text);
    check(!/In flight0/.test(tiles), 'frozen current zero', tiles);
    check(!input.feed.some(r => r.state === 'in-flight'), 'frozen row liveness', input.feed);
  }
  if (dimension === 'source-boundary') {
    const sid = snapshot.fixtureScenario.inputs.source;
    if (sid === 'windows-worker') {
      check((snapshot.windowsWorker.state === 'advertised') === e.retained, 'worker heartbeat retained', snapshot.windowsWorker);
      const node = api.graph.nodes.find(n => n.id === 'windows-worker');
      check(api.graph.nodes.some(n => n.kind === 'windows-lane') === e.current, 'exact heartbeat lane visibility', node?.windowsLanes);
    } else {
      check(Boolean(snapshot.windowsLive) === e.retained, 'peer report retained', snapshot.windowsLive);
      const view = layout.pcReportView(snapshot.windowsLive, {feedFresh:true, snapshotAge:snapshot.fixtureScenario.inputs.elapsed});
      check(view.known === e.current, 'exact peer report clock', view);
    }
  }
  if (dimension === 'history-read') {
    if (e.current) {
      check(tiles.includes(`Routes${e.routes}`) && !/Routes(?:Unknown|at least)/.test(tiles), 'exact history count after read', tiles);
      check(tiles.includes('Failed0'), 'exact history failures', tiles);
      check($('activityStatus').textContent === 'Nothing running now', 'exact history inactivity', $('activityStatus').textContent);
    } else {
      check(/RoutesUnknown/.test(tiles), 'incomplete history count', tiles);
      check(!/Nothing running now|In flight0/.test($('activityStatus').textContent+tiles), 'incomplete history inactivity', tiles);
    }
  }
  if (dimension === 'fast-overlay') {
    const sid = snapshot.fixtureScenario.inputs.source, source = snapshot.sources.find(s => s.id === sid);
    check(Math.abs(source.observationAgeSeconds-e.age) < .000001, 'overlay observation clock', source);
    if (sid === 'pipeline-router') check(/^Running/.test(input.vitals.route.detail) === e.current, 'overlay route chip', input.vitals.route);
    if (sid === 'router-active') check(input.feed.some(r => r.kind === 'route' && r.state === 'in-flight') === e.current, 'overlay route liveness', input.feed);
    if (sid === 'windows-jobs') check(input.feed.some(r => r.kind === 'pc-job' && r.state === 'in-flight') === e.current, 'overlay job liveness', input.feed);
  }
  if (dimension === 'journal-read') {
    if (e.current) check(tiles.includes(`Answered${e.answered}`) && tiles.includes('In flight0'), 'exact readable journal', tiles);
    else check(/AnsweredUnknown/.test(tiles) && !/In flight0/.test(tiles), 'malformed journal never exact', tiles);
  }
}
