import { execFileSync } from 'node:child_process';
import { appendFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const SHA = /^[a-f0-9]{40}$/i;
const annotations = {
	source: 'org.opencontainers.image.source',
	revision: 'org.opencontainers.image.revision',
	version: 'org.opencontainers.image.version'
};

function run(command, args, options = {}) {
	try {
		return execFileSync(command, args, {
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'pipe'],
			...options
		}).trim();
	} catch (error) {
		const detail = `${error.stderr ?? ''} ${error.stdout ?? ''}`.trim();
		throw new Error(`${command} ${args[0]} failed${detail ? `: ${detail}` : ''}`);
	}
}
function semver(value) {
	if (!VERSION.test(value ?? '')) throw new Error(`Invalid release version: ${value}`);
	return value;
}
function sha(value) {
	if (!SHA.test(value ?? '')) throw new Error(`Invalid commit SHA: ${value}`);
	return value.toLowerCase();
}
function repository(env) {
	const repo = env.GITHUB_REPOSITORY ?? '';
	if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo))
		throw new Error('GITHUB_REPOSITORY must be owner/repository');
	return repo;
}
function packageVersion(cwd = process.cwd()) {
	const helper = fileURLToPath(new URL('./release_version.py', import.meta.url));
	return semver(run('uv', ['run', '--no-project', '--python', '3.13', 'python', helper, cwd]));
}
function validateVersion(env = process.env, readVersion = packageVersion) {
	const version = readVersion();
	const branch = env.GITHUB_EVENT_NAME === 'pull_request' ? env.GITHUB_HEAD_REF : env.GITHUB_REF_NAME;
	if (branch?.startsWith('releases/') && branch !== `releases/${version}`)
		throw new Error('Release branch version does not match package metadata');
	output({version}, env);
}

function output(values, env = process.env) {
	const lines =
		Object.entries(values)
			.map(([key, value]) => `${key}=${String(value).replace(/[\r\n]/g, '')}`)
			.join('\n') + '\n';
	if (env.GITHUB_OUTPUT) appendFileSync(env.GITHUB_OUTPUT, lines);
	else process.stdout.write(lines);
}
function gh(env, endpoint, runner = run, args = []) {
	if (!env.GH_TOKEN) throw new Error('GH_TOKEN is required');
	return JSON.parse(
		runner('gh', ['api', endpoint, ...args], { env: { ...process.env, GH_TOKEN: env.GH_TOKEN } })
	);
}
function trustedMain(env) {
	return (
		['push', 'workflow_dispatch'].includes(env.GITHUB_EVENT_NAME) &&
		env.GITHUB_REF === 'refs/heads/main'
	);
}
function currentMain(env, runner = run) {
	const ref = gh(env, `repos/${repository(env)}/git/ref/heads/main`, runner);
	return sha(env.GITHUB_SHA) === sha(ref.object?.sha);
}
function mergedRelease(env, runner = run) {
	const repo = repository(env),
		commitSha = sha(env.GITHUB_SHA);
	const pages = gh(env, `repos/${repo}/commits/${commitSha}/pulls`, runner, [
		'--paginate',
		'--slurp'
	]);
	return (Array.isArray(pages) ? pages.flat() : []).find(
		(pr) =>
			pr.merged_at &&
			pr.base?.ref === 'main' &&
			pr.base?.repo?.full_name?.toLowerCase() === repo.toLowerCase() &&
			pr.head?.repo?.full_name?.toLowerCase() === repo.toLowerCase() &&
			/^releases\/(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(pr.head?.ref ?? '') &&
			pr.merge_commit_sha?.toLowerCase() === commitSha
	);
}
function planRc(env = process.env, readVersion = packageVersion) {
	const repo = repository(env),
		branch = env.GITHUB_REF_NAME ?? '';
	if (
		!['push', 'workflow_dispatch'].includes(env.GITHUB_EVENT_NAME) ||
		!/^refs\/heads\/releases\/(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(
			env.GITHUB_REF ?? ''
		) ||
		env.GITHUB_REF !== `refs/heads/${branch}`
	)
		throw new Error('RC plan requires a release branch push or workflow_dispatch');
	const version = semver(branch.slice('releases/'.length));
	if (readVersion() !== version)
		throw new Error('Release branch version does not match package metadata');
	const runNumber = Number(env.GITHUB_RUN_NUMBER);
	if (!Number.isSafeInteger(runNumber) || runNumber < 1)
		throw new Error('GITHUB_RUN_NUMBER must be a positive integer');
	output(
		{
			image: `ghcr.io/${repo.toLowerCase()}`,
			version,
			tag: `${version}-rc.${runNumber}`,
			sha: sha(env.GITHUB_SHA),
			eligible: 'true'
		},
		env
	);
}
function planFinal(env = process.env, runner = run, readVersion = packageVersion) {
	const repo = repository(env);
	if (!trustedMain(env) || !currentMain(env, runner)) return output({ eligible: 'false' }, env);
	const pr = mergedRelease(env, runner);
	if (!pr) return output({ eligible: 'false' }, env);
	const version = semver(pr.head.ref.slice('releases/'.length));
	if (readVersion() !== version)
		throw new Error('Merged release version does not match package metadata');
	output(
		{
			eligible: 'true',
			image: `ghcr.io/${repo.toLowerCase()}`,
			version,
			tag: version,
			sha: sha(env.GITHUB_SHA)
		},
		env
	);
}
function missingManifest(error, ref) {
	const message = error.message
		.trim()
		.replace(/^docker buildx failed:\s*/, '')
		.replace(/^ERROR:\s*/, '');
	return (
		/^(?:manifest unknown|no such manifest|name unknown)$/i.test(message) ||
		message === `${ref}: not found`
	);
}
function inspect(image, tag, runner = run) {
	const ref = `${image}:${tag}`;
	let first;
	try {
		first = runner('docker', ['buildx', 'imagetools', 'inspect', ref]);
	} catch (error) {
		if (missingManifest(error, ref)) return null;
		throw error;
	}
	const digest = first.match(/^Digest:\s*(sha256:[a-f0-9]{64})/m)?.[1];
	if (!digest) throw new Error('Image inspect did not return a digest');
	const raw = runner('docker', ['buildx', 'imagetools', 'inspect', '--raw', `${image}@${digest}`]);
	return { digest, manifest: JSON.parse(raw) };
}
function checkImage({ image, tag, sha: commit, version, repo }, runner = run) {
	const found = inspect(image, tag, runner);
	if (!found) return { exists: false };
	const ann = found.manifest.annotations ?? {};
	if (
		ann[annotations.revision] !== commit ||
		ann[annotations.source] !== `https://github.com/${repo}` ||
		ann[annotations.version] !== version
	)
		throw new Error(`Image tag collision: ${image}:${tag} has different release metadata`);
	const platforms = new Set(
		(found.manifest.manifests ?? []).map(
			(item) => `${item.platform?.os}/${item.platform?.architecture}`
		)
	);
	if (!platforms.has('linux/amd64') || !platforms.has('linux/arm64'))
		throw new Error(`Image ${image}:${tag} is missing required platforms`);
	return { exists: true, digest: found.digest };
}
function verifyProvenance(env, digest, isFinal, runner = run) {
	const repo = repository(env);
	if (!env.GH_TOKEN) throw new Error('GH_TOKEN is required for provenance verification');
	runner('gh', [
		'attestation', 'verify', `oci://${env.RELEASE_IMAGE}@${digest}`,
		'--repo', repo,
		'--signer-workflow', `${repo}/.github/workflows/${isFinal ? 'publish' : 'docker'}.yml`,
		'--source-digest', sha(env.GITHUB_SHA),
		'--source-ref', env.GITHUB_REF,
		'--deny-self-hosted-runners'
	], {env: {...process.env, GH_TOKEN: env.GH_TOKEN}});
}
function imageStatus(env = process.env, runner = run) {
	const repo = repository(env),
		tag = env.RELEASE_TAG ?? '';
	const match = tag.match(/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?$/);
	if (!match) throw new Error('Invalid release image tag');
	const baseVersion = match.slice(1, 4).join('.'),
		isFinal = !match[4],
		commit = sha(env.RELEASE_SHA);
	const image = `ghcr.io/${repo.toLowerCase()}`;
	if (commit !== sha(env.GITHUB_SHA))
		throw new Error('Release source SHA does not match the workflow commit');
	if (!isFinal) {
		const branch = `releases/${baseVersion}`;
		if (!['push', 'workflow_dispatch'].includes(env.GITHUB_EVENT_NAME) ||
			env.GITHUB_REF !== `refs/heads/${branch}` || env.GITHUB_REF_NAME !== branch ||
			!Number.isSafeInteger(Number(env.GITHUB_RUN_NUMBER)) || Number(env.GITHUB_RUN_NUMBER) < 1 ||
			match[4] !== String(Number(env.GITHUB_RUN_NUMBER)))
			throw new Error('RC image requires the matching release branch and workflow run number');
	}
	if (env.RELEASE_IMAGE !== image)
		throw new Error('RELEASE_IMAGE does not match GITHUB_REPOSITORY');
	if (isFinal) {
		if (!trustedMain(env) || !currentMain(env, runner))
			throw new Error('Final release source is not current main');
		const pr = mergedRelease(env, runner);
		if (!pr || pr.head.ref !== `releases/${baseVersion}`)
			throw new Error('Final release requires the matching merged release PR');
		const refs = gh(env, `repos/${repo}/git/matching-refs/tags/v`, runner);
		if (refs.some((ref) => ref.ref === `refs/tags/v${tag}`)) {
			const existing = gh(env, `repos/${repo}/git/ref/tags/v${tag}`, runner);
			if (existing.object?.type !== 'commit' || sha(existing.object?.sha) !== commit)
				throw new Error(`Refusing existing Git tag v${tag}`);
		}
	}
	const result = checkImage({ image, tag, sha: commit, version: tag, repo }, runner);
	if (
		isFinal &&
		!result.exists &&
		gh(env, `repos/${repo}/git/matching-refs/tags/v`, runner).some(
			(ref) => ref.ref === `refs/tags/v${tag}`
		)
	)
		throw new Error(`Final image ${tag} is missing for an existing Git tag`);
	if (result.exists) verifyProvenance(env, result.digest, isFinal, runner);
	output(
		{ exists: String(result.exists), ...(result.digest ? { digest: result.digest } : {}) },
		env
	);
	return result;
}
function compareVersions(a, b) {
	const left = a.split('.').map(Number),
		right = b.split('.').map(Number);
	for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] - right[i];
	return 0;
}
function finishFinal(env = process.env, runner = run, readVersion = packageVersion) {
	const repo = repository(env),
		version = semver(env.RELEASE_TAG),
		commit = sha(env.RELEASE_SHA);
	if (env.RELEASE_IMAGE !== `ghcr.io/${repo.toLowerCase()}`)
		throw new Error('RELEASE_IMAGE does not match GITHUB_REPOSITORY');
	if (!trustedMain(env) || !currentMain(env, runner) || commit !== sha(env.GITHUB_SHA))
		throw new Error('Final release source is not current main');
	const pr = mergedRelease(env, runner);
	if (!pr || pr.head.ref !== `releases/${version}`)
		throw new Error('Final release requires the matching merged release PR');
	if (readVersion() !== version)
		throw new Error('Merged release version does not match package metadata');
	const refs = gh(env, `repos/${repo}/git/matching-refs/tags/v`, runner);
	const existing = refs.find((ref) => ref.ref === `refs/tags/v${version}`);
	if (existing) {
		const tagRef = gh(env, `repos/${repo}/git/ref/tags/v${version}`, runner);
		if (tagRef.object?.type !== 'commit' || sha(tagRef.object?.sha) !== commit)
			throw new Error(`Refusing existing Git tag v${version}`);
	}
	const newer = refs
		.map(
			(ref) => ref.ref.match(/^refs\/tags\/v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$/)?.[1]
		)
		.filter(Boolean)
		.some((tag) => compareVersions(tag, version) > 0);
	if (newer) throw new Error('A newer final version already exists');
	const image = checkImage(
		{ image: env.RELEASE_IMAGE, tag: version, sha: commit, version, repo },
		runner
	);
	if (!image.exists || env.IMAGE_DIGEST !== image.digest)
		throw new Error('Final image digest is missing or does not match the verified image');
	verifyProvenance(env, image.digest, true, runner);
	if (!existing)
		gh(env, `repos/${repo}/git/refs`, runner, [
			'-X',
			'POST',
			'-f',
			`ref=refs/tags/v${version}`,
			'-f',
			`sha=${commit}`
		]);
	let hasRelease = true;
	try {
		runner('gh', ['release', 'view', `v${version}`, '--repo', repo], {
			env: { ...process.env, GH_TOKEN: env.GH_TOKEN }
		});
	} catch (error) {
		if (!/release not found|404/i.test(error.message)) throw error;
		hasRelease = false;
	}
	if (!hasRelease) {
		const notes = `Source: ${commit}\nImage: ${env.RELEASE_IMAGE}@${image.digest}\nWorkflow run: https://github.com/${repo}/actions/runs/${env.GITHUB_RUN_ID ?? ''}`;
		runner(
			'gh',
			[
				'release',
				'create',
				`v${version}`,
				'--repo',
				repo,
				'--verify-tag',
				'--title',
				`Release ${version}`,
				'--notes',
				notes,
				'--latest=false'
			],
			{ env: { ...process.env, GH_TOKEN: env.GH_TOKEN } }
		);
	}
	if (!currentMain(env, runner)) throw new Error('main moved before updating latest');
	runner('docker', [
		'buildx',
		'imagetools',
		'create',
		'--prefer-index=false',
		'--tag',
		`${env.RELEASE_IMAGE}:latest`,
		`${env.RELEASE_IMAGE}@${image.digest}`
	]);
	const latest = inspect(env.RELEASE_IMAGE, 'latest', runner);
	if (!latest || latest.digest !== image.digest)
		throw new Error('latest does not point to the final release image');
	runner('gh', ['release', 'edit', `v${version}`, '--repo', repo, '--latest'], {
		env: { ...process.env, GH_TOKEN: env.GH_TOKEN }
	});
}
export { semver, packageVersion, validateVersion, planRc, planFinal, checkImage, imageStatus, finishFinal };
const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) {
	const commands = {
		'validate-version': validateVersion,
		'plan-rc': planRc,
		'plan-final': planFinal,
		'image-status': imageStatus,
		'finish-final': finishFinal
	};
	try {
		const command = commands[process.argv[2]];
		if (!command || process.argv.length !== 3)
			throw new Error(
				'Usage: node scripts/release.mjs <validate-version|plan-rc|plan-final|image-status|finish-final>'
			);
		command();
	} catch (error) {
		process.stderr.write(`${error.message}\n`);
		process.exitCode = 1;
	}
}
