// Called by actions/github-script; kept separate so issue handling can be tested offline.
module.exports = async ({github, context, env = process.env}) => {
    const {REPO, KEY, CURRENT, NEW_REF, VERIFICATION, MIN_SERVER, SERVER_TAG, BRANCH} = env;
    if (!['clean', 'conflict'].includes(VERIFICATION)) {
        throw new Error('Refusing an upstream alert without a completed patch check');
    }
    const target = {owner: context.repo.owner, repo: context.repo.repo};
    const begin = `<!-- maintainedmost-upstream:${KEY} -->`;
    const end = '<!-- /maintainedmost-upstream -->';
    const title = BRANCH ? `${REPO} ${BRANCH} advanced to ${NEW_REF.slice(0, 12)}` : `${REPO} ${NEW_REF} is available`;
    const result = VERIFICATION === 'clean'
        ? 'The complete patch series applies sequentially. This is not a build, test, or compatibility verdict.'
        : '**The combined patch series does not apply.** Rebase and review the failing patch; do not drop security or feature patches to make the build pass.';
    let compatibility = 'Check server, plugin and job-image compatibility before changing any pins.';
    if (KEY === 'CALLS_TAG') {
        if (MIN_SERVER && MIN_SERVER !== 'unknown') {
            const minimum = MIN_SERVER.split('.').map(Number);
            const current = SERVER_TAG.replace(/^v/, '').split('.').map(Number);
            const firstDifference = minimum.findIndex((value, index) => value !== current[index]);
            const incompatible = firstDifference >= 0 && minimum[firstDifference] > current[firstDifference];
            compatibility = `Minimum Mattermost server: \`${MIN_SERVER}\`; pinned server: \`${SERVER_TAG}\`. ` +
                (incompatible ? '**Do not upgrade Calls on the pinned server.** Upgrade and validate the server first.' : 'Review the remaining compatibility requirements before upgrading Calls.');
        } else {
            compatibility = '**Minimum server compatibility is unknown. Check the upstream plugin manifest and release notes before upgrading Calls.**';
        }
    }
    const log = `https://github.com/${target.owner}/${target.repo}/actions/runs/${context.runId}`;
    const managed = `${begin}\n\`${REPO}\` moved from \`${CURRENT}\` to \`${NEW_REF}\`${BRANCH ? ` on \`${BRANCH}\`` : ''}.\n\n` +
        `${result}\n\n${compatibility}\n\n` +
        `Review the [workflow log](${log}) and [upstream changes](https://github.com/${REPO}/compare/${CURRENT}...${NEW_REF}). ` +
        `Update \`${KEY}\` and its commit/image pins in \`upstream.env\` only after review, then run the complete CI tests and builds. ` +
        'For Calls, also review the bundle version. This workflow never upgrades dependencies automatically.\n' + end;

    // Forks may not have the label yet. Propagate permission/network failures.
    try {
        await github.rest.issues.getLabel({...target, name: 'upstream'});
    } catch (error) {
        if (error.status !== 404) throw error;
        try {
            await github.rest.issues.createLabel({...target, name: 'upstream', color: '0366d6'});
        } catch (createError) {
            if (createError.status !== 422) throw createError;
        }
    }

    const issues = await github.paginate(github.rest.issues.listForRepo, {...target, state: 'open', per_page: 100});
    const matches = issues.filter((issue) => !issue.pull_request && issue.user?.login === 'github-actions[bot]' && (
        issue.body?.includes(begin) ||
        (issue.title.startsWith(`${REPO} `) && issue.title.endsWith(' is available'))
    )).sort((a, b) => a.number - b.number);
    let number;
    if (matches.length) {
        const issue = matches[0];
        const body = issue.body || '';
        const start = body.indexOf(begin);
        const finish = body.indexOf(end, start);
        // Only replace our delimited section; preserve maintainer notes verbatim.
        const updated = start >= 0 && finish >= 0
            ? body.slice(0, start) + managed + body.slice(finish + end.length)
            : managed + (body ? `\n\n<details><summary>Previous alert (superseded)</summary>\n\n${body}\n\n</details>` : '');
        number = issue.number;
        if (issue.title !== title || body !== updated) {
            await github.rest.issues.update({...target, issue_number: number, title, body: updated});
        }
    } else {
        const created = await github.rest.issues.create({...target, title, body: managed, labels: ['upstream']});
        number = created.data.number;
    }
    for (const duplicate of matches.slice(1)) {
        await github.rest.issues.createComment({...target, issue_number: duplicate.number, body: `Tracking this upstream in #${number}. Closing the duplicate alert; its discussion is retained here.`});
        await github.rest.issues.update({...target, issue_number: duplicate.number, state: 'closed'});
    }
};
