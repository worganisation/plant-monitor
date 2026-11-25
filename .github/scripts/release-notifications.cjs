const { createHash } = require('node:crypto')
const { setTimeout: sleep } = require('node:timers/promises')

/** Escape text before adding it to Slack markup. */
function escape(text) {
  return String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

/** Render the changelog for Slack, keeping PR links and omitting duplicate release metadata. */
function notes(body) {
  return escape(body || '').split(/\n---\s*\n+\*\*Detailed Changes\*\*:/i)[0]
    .replace(/^## v?\d+\.\d+\.\d+[^\n]*$/gm, '')
    .replace(/, \[`[a-f0-9]+`\]\(https:\/\/github\.com\/[^\s)]+\/commit\/[a-f0-9]+\)/gi, '')
    .replace(/^#{1,6} (.+)$/gm, '*$1*').replace(/\*\*([^*\n]+)\*\*/g, '*$1*')
    .replace(/^[-*] /gm, '• ').replace(/\[([^\]\n]+)\]\((https:\/\/github\.com\/[^\s)<>]+)\)/g, '<$2|$1>')
    .replace(/^(\*[^*\n]+\*)\n\n(?=•)/gm, '$1\n').trim()
}

/** Send a Slack request and surface API errors without logging credentials. */
async function slack(method, payload) {
  for (let attempt = 1; ; attempt++) {
    const response = await fetch('https://slack.com/api/' + method, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000),
      headers: { Authorization: 'Bearer ' + process.env.SLACK_TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (response.status === 429 && attempt < 4) {
      const seconds = Number(response.headers.get('retry-after') || 1)
      if (!Number.isFinite(seconds) || seconds < 0 || seconds > 60) throw new Error('Slack rate limit exceeds retry budget')
      await sleep(Math.max(seconds, 1) * 1000)
      continue
    }

    if (!response.ok) throw new Error('Slack HTTP ' + response.status)
    const result = await response.json()
    if (!result.ok) throw new Error('Slack ' + method + ': ' + result.error)
    return result
  }
}

/** Create a message once per release asset or update the recorded Slack message on retry. */
async function publish(github, repo, release, key, text, thread) {
  const name = 'slack-' + key + '.json'
  const channel = process.env.SLACK_CHANNEL
  const asset = release.assets.find(item => item.name === name)
  if (asset) {
    const { data } = await github.rest.repos.getReleaseAsset({
      ...repo, asset_id: asset.id, headers: { accept: 'application/octet-stream' },
    })
    const saved = JSON.parse(Buffer.from(data).toString())
    if (saved.channel !== channel || !/^\d+\.\d+$/.test(saved.ts)) throw new Error('Invalid Slack thread receipt')
    await slack('chat.update', { channel, ts: saved.ts, text, unfurl_links: false, unfurl_media: false })
    return saved.ts
  }

  const id = createHash('sha256').update(repo.owner + '/' + repo.repo + '/' + release.id + '/' + key).digest('hex')
  const clientId = [id.slice(0, 8), id.slice(8, 12), '4' + id.slice(13, 16), '8' + id.slice(17, 20), id.slice(20, 32)].join('-')
  const posted = await slack('chat.postMessage', {
    channel, text, thread_ts: thread, client_msg_id: clientId, unfurl_links: false, unfurl_media: false,
  })
  await github.rest.repos.uploadReleaseAsset({
    ...repo, release_id: release.id, name,
    headers: { 'content-type': 'application/octet-stream' },
    data: Buffer.from(JSON.stringify({ channel, ts: posted.ts })),
  })
  return posted.ts
}

/** Format a material stage with a stable receipt across attempts of the same run. */
function stageReply(run, job, step) {
  const stage = step || job
  const identity = JSON.stringify([job.name, step?.name])
  const digest = createHash('sha256').update(identity).digest('hex').slice(0, 24)
  const failed = step ? [] : (job.steps || []).filter(item => item.conclusion === 'failure')
  const details = failed.map(item => escape(item.name)).join(', ')
  const title = step ? step.name : job.name
  const outcomes = { success: 'completed', failure: 'failed', cancelled: 'was cancelled', skipped: 'was skipped', timed_out: 'timed out' }
  const text = '*' + escape(title) + '* ' + (outcomes[stage.conclusion] || escape(stage.conclusion)) + '.'
    + (details ? '\nFailed step: ' + details : '')
    + '\n<' + (job.html_url || run.html_url) + '|View workflow log>'
  return { key: 'run-' + run.id + '-' + digest, text, skipped: stage.conclusion === 'skipped' }
}

/** Identify the release and material jobs from a completed workflow attempt. */
async function workflowOutcome(github, repo, run) {
  const { data: latest } = await github.rest.actions.getWorkflowRun({ ...repo, run_id: run.id })
  if (latest.run_attempt !== run.run_attempt || latest.status !== 'completed') return

  const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
    ...repo, run_id: run.id, filter: 'latest', per_page: 100,
  })
  let tag = run.head_branch
  let material = jobs
  if (run.event === 'push') {
    tag = jobs.map(job => /(?:^| \/ )Draft Release (\d+\.\d+\.\d+)$/.exec(job.name)?.[1]).find(Boolean)
    material = material.filter(job => /Build Documentation|Deploy Documentation|Run Production Build|Deploy to (Production|Development)/i.test(job.name) && !job.name.endsWith('Log Inputs'))
  } else if (run.event === 'workflow_dispatch') {
    const marker = /^Deploy release (\d+\.\d+\.\d+)(?: \/ |$)/
    tag = jobs.map(job => marker.exec(job.name)?.[1]).find(Boolean) || marker.exec(run.display_title)?.[1]
    material = material.filter(job => !/^(Create release|Release)$/.test(job.name))
  }

  if (!tag || (!material.length && run.conclusion === 'success')) return
  const replies = []
  for (const job of material) {
    // A deployment job can build a model or publish an image before it reaches the target.
    for (const step of job.steps || []) {
      const embeddedBuild = /^build(?: |$)/i.test(step.name) && !/build/i.test(job.name)
      const publication = /^(?:publish|push) (?:.* )?(?:image|package|wheel|artifact)s?\b/i.test(step.name)
      if (embeddedBuild || publication) replies.push(stageReply(run, job, step))
    }
    replies.push(stageReply(run, job))
  }
  if (!material.length) replies.push(stageReply(run, { name: run.name, conclusion: run.conclusion }))
  return { tag, replies }
}

/** Announce the published release and attach correlated deployment or build evidence. */
module.exports = async function report({ github, context, core }) {
  const repo = context.repo
  const event = context.payload
  let tag = event.release?.tag_name
  let replies = []
  if (event.workflow_run) {
    const outcome = await workflowOutcome(github, repo, event.workflow_run)
    if (!outcome) return
    tag = outcome.tag
    replies = outcome.replies
  } else if (context.eventName === 'repository_dispatch') {
    const payload = event.client_payload
    const product = repo.repo === 'home-assistant' ? 'Home Assistant' : 'AppDaemon'
    const version = 'v' + payload?.tag
    const messages = {
      checked_out: product + ' checked out ' + version + '.',
      skipped: product + ' stayed on the selected branch. Version ' + version + ' was skipped.',
      failed: product + ' could not check out ' + version + '.',
    }
    if (repo.repo === 'home-assistant') {
      messages.ha_started = 'Home Assistant started with ' + version + '.'
      messages.automations_reloaded = 'Home Assistant reloaded its automations with ' + version + '.'
    } else if (repo.repo === 'home-assistant-appdaemon') {
      messages.app_runtime = '*AppDaemon is online*'
    } else {
      throw new Error('Unexpected checkout reporter')
    }
    if (!/^\d+\.\d+\.\d+$/.test(payload?.tag) || !Object.hasOwn(messages, payload?.status)) {
      throw new Error('Invalid checkout report')
    }
    tag = payload.tag
    let message = messages[payload.status]
    let key = 'checkout'
    if (!['skipped', 'failed'].includes(payload.status)) {
      const { data: commit } = await github.rest.repos.getCommit({ ...repo, ref: tag })
      if (commit.sha !== payload.sha) throw new Error('Reported checkout does not match the release commit')
      if (payload.status !== 'checked_out') key = payload.status
    }
    if (payload.status === 'app_runtime') {
      if (![payload.active, payload.inactive].every(count => Number.isSafeInteger(count) && count >= 0)) {
        throw new Error('Invalid AppDaemon runtime counts')
      }
      message += '\n' + payload.active + (payload.active === 1 ? ' app active' : ' apps active') + ' · ' + payload.inactive + ' inactive'
      const apps = payload.initialized
      if (!Array.isArray(apps) || !apps.every(app => typeof app === 'string')) {
        throw new Error('Invalid AppDaemon initialized apps')
      }
      if (apps.length) message += '\nStarted after the update: ' + apps.map(app => escape(app.replaceAll('_', ' '))).join(', ')
    }
    replies.push({ key, text: message })
  }

  if (!tag) throw new Error('Missing release tag')
  let release
  // GitHub can deliver an on-host checkout or tag build before the release is published.
  for (let attempt = 0; attempt < 25; attempt++) {
    try {
      const result = await github.rest.repos.getReleaseByTag({ ...repo, tag })
      if (!result.data.draft) { release = result.data; break }
    } catch (error) {
      if (error.status !== 404) throw error
    }
    await sleep(10000)
  }
  if (!release) throw new Error('No published release for ' + tag)
  if (release.immutable) throw new Error('Release is immutable; cannot persist Slack thread receipt')
  const names = { 'home-assistant': 'Home Assistant', 'home-assistant-appdaemon': 'AppDaemon' }
  const heading = '*<' + release.html_url + '|' + escape(names[repo.repo] || repo.repo) + ' ' + escape(tag) + '>*'
  const body = notes(release.body)
  const parent = await publish(github, repo, release, 'release', heading + (body ? '\n\n' + body.slice(0, 35000) : ''))
  for (const reply of replies) {
    if (reply.skipped && !release.assets.some(asset => asset.name === 'slack-' + reply.key + '.json')) continue
    await publish(github, repo, release, reply.key, reply.text, parent)
  }
  core.info('Reported ' + repo.repo + ' ' + tag)
}
