import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import {
	semver,
	packageVersion,
	validateVersion,
	planRc,
	planFinal,
	checkImage,
	imageStatus,
	finishFinal
} from './release.mjs';

const commit = 'a'.repeat(40),
	parent = 'b'.repeat(40),
	mergeParent = 'd'.repeat(40),
	digest = `sha256:${'c'.repeat(64)}`;
const version = '1.0.0',
	repo = 'owner/repository',
	image = 'ghcr.io/owner/repository';
const baseEnv = {
	GITHUB_REPOSITORY: repo,
	GITHUB_REF: 'refs/heads/main',
	GITHUB_REF_NAME: 'main',
	GITHUB_EVENT_NAME: 'push',
	GITHUB_SHA: commit,
	RELEASE_SHA: commit,
	RELEASE_TAG: version,
	RELEASE_IMAGE: image,
	IMAGE_DIGEST: digest,
	GH_TOKEN: 'test-token'
};
const pr = {
	merged_at: '2026-01-01T00:00:00Z',
	base: { ref: 'main', repo: { full_name: repo } },
	head: { ref: `releases/${version}`, sha: parent, repo: { full_name: repo } },
	merge_commit_sha: commit
};
const manifest = {
	annotations: {
		'org.opencontainers.image.revision': commit,
		'org.opencontainers.image.source': `https://github.com/${repo}`,
		'org.opencontainers.image.version': version
	},
	manifests: [
		{ platform: { os: 'linux', architecture: 'amd64' } },
		{ platform: { os: 'linux', architecture: 'arm64' } }
	]
};
const output = () => {
	const values = [];
	return {
		values,
		write: (value) => {
			values.push(value);
			return true;
		}
	};
};
function withOutput(fn) {
	const old = process.stdout.write,
		result = output();
	process.stdout.write = result.write;
	try {
		fn();
	} finally {
		process.stdout.write = old;
	}
	return result.values.join('');
}
function writeVersions(cwd, project = version, lock = version, api = version) {
	writeFileSync(path.join(cwd, 'pyproject.toml'), `[project]\nname = "cashier-server"\nversion = "${project}"\n`);
	writeFileSync(path.join(cwd, 'uv.lock'), `version = 1\n[[package]]\nname = "cashier-server"\nversion = "${lock}"\nsource = { editable = "." }\n`);
	writeFileSync(path.join(cwd, 'main.py'), `from fastapi import FastAPI\napp = FastAPI(version="${api}")\n`);
}
function fixture() {
	const cwd = mkdtempSync(path.join(tmpdir(), 'release-test-'));
	writeVersions(cwd);
	return { cwd, clean: () => rmSync(cwd, { recursive: true, force: true }) };
}

function makeRunner({
	tags = [],
	imagePresent = true,
	latestPresent = true,
	moved = false,
	moveAfterFirstMain = false,
	releaseExists = false,
	tagType = 'commit',
	tagSha = commit,
	manifestValue = manifest,
	associatedPrs = [pr]
} = {}) {
	const calls = [];
	let mainReads = 0;
	const runner = (command, args) => {
		calls.push([command, args]);
		if (command === 'gh' && args[0] === 'api') {
			const endpoint = args[1];
			if (endpoint === `repos/${repo}/git/ref/heads/main`) {
				mainReads++;
				return JSON.stringify({
					object: { sha: moved || (moveAfterFirstMain && mainReads > 1) ? parent : commit }
				});
			}
			if (endpoint === `repos/${repo}/commits/${commit}/pulls`)
				return JSON.stringify([associatedPrs]);
			if (endpoint === `repos/${repo}/git/matching-refs/tags/v`)
				return JSON.stringify(tags.map((tag) => ({ ref: `refs/tags/v${tag}` })));
			if (endpoint === `repos/${repo}/git/refs`) return '{}';
			if (endpoint === `repos/${repo}/git/ref/tags/v${version}`)
				return JSON.stringify({ object: { type: tagType, sha: tagSha } });
		}
		if (command === 'gh' && args[0] === 'attestation') return 'verified';
		if (command === 'gh' && args[0] === 'release') {
			if (args[1] === 'view' && releaseExists) return 'ok';
			if (args[1] === 'create' && releaseExists) throw Error('release already exists');
			if (args[1] === 'view') throw Error('release not found (404)');
			return 'created';
		}
		if (command === 'docker' && args.includes('inspect')) {
			const raw = args.includes('--raw');
			if (raw) return JSON.stringify(manifestValue);
			if (args.at(-1).endsWith(':latest') && !latestPresent) throw Error('manifest unknown');
			if (args.at(-1).endsWith(`:${version}`) && !imagePresent) throw Error('manifest unknown');
			return `Digest: ${digest}`;
		}
		if (command === 'docker' && args.includes('create')) return 'created';
		throw Error(`unexpected ${command} ${args.join(' ')}`);
	};
	return { runner, calls };
}

test('image-status rejects mismatched source SHA before registry access', () => {
	for (const tag of [version, `${version}-rc.19`]) {
		const {runner, calls} = makeRunner();
		assert.throws(() => imageStatus({...baseEnv, RELEASE_TAG: tag, RELEASE_SHA: parent}, runner), /source SHA/);
		assert.equal(calls.length, 0);
	}
});
test('RC image-status requires its release branch event and exact run number', () => {
	const env = {...baseEnv, RELEASE_TAG: `${version}-rc.19`,
		GITHUB_REF: `refs/heads/releases/${version}`,
		GITHUB_REF_NAME: `releases/${version}`, GITHUB_RUN_NUMBER: '19'};
	for (const changed of [
		{GITHUB_EVENT_NAME:'pull_request'}, {GITHUB_REF:'refs/heads/main'},
		{GITHUB_REF_NAME:'releases/2.0.0'}, {GITHUB_RUN_NUMBER:'20'},
		{GITHUB_RUN_NUMBER:'0'}, {GITHUB_RUN_NUMBER:'19.5'}
	]) {
		const {runner, calls} = makeRunner();
		assert.throws(() => imageStatus({...env,...changed},runner), /matching release branch/);
		assert.equal(calls.length,0);
	}
});
test('existing images require a digest-bound signature from the expected workflow and source', () => {
	for (const final of [true,false]) {
		const tag = final ? version : `${version}-rc.19`;
		const env = {...baseEnv,RELEASE_TAG:tag,...(final ? {} : {
			GITHUB_REF:`refs/heads/releases/${version}`, GITHUB_REF_NAME:`releases/${version}`, GITHUB_RUN_NUMBER:'19'
		})};
		const manifestValue={...manifest,annotations:{...manifest.annotations,'org.opencontainers.image.version':tag}};
		const {runner,calls}=makeRunner({manifestValue});
		withOutput(() => imageStatus(env,runner));
		const verification=calls.find(([command,args])=>command==='gh'&&args[0]==='attestation');
		assert.deepEqual(verification,['gh',[
			'attestation','verify',`oci://${image}@${digest}`,'--repo',repo,
			'--signer-workflow',`${repo}/.github/workflows/${final?'publish':'docker'}.yml`,
			'--source-digest',commit,'--source-ref',env.GITHUB_REF,'--deny-self-hosted-runners'
		]]);
		assert.throws(()=>imageStatus(env,(command,args)=>{
			if(command==='gh'&&args[0]==='attestation') throw Error('invalid signature');
			return runner(command,args);
		}), /invalid signature/);
	}
});
test('finish-final never writes tags or latest if signature verification fails', () => {
	const {runner,calls}=makeRunner();
	assert.throws(()=>finishFinal(baseEnv,(command,args)=>{
		if(command==='gh'&&args[0]==='attestation') throw Error('untrusted image');
		return runner(command,args);
	},()=>version), /untrusted image/);
	assert.equal(calls.some(([,args])=>args.includes('POST')||args[0]==='release'||args.includes('create')),false);
});

test('accepts strict stable semantic versions only', () => {
	assert.equal(semver('1.2.3'), '1.2.3');
	for (const invalid of ['01.2.3', '1.02.3', 'v1.2.3', '1.2', '1.2.3-rc.1'])
		assert.throws(() => semver(invalid), /Invalid release version/);
});
test('packageVersion rejects mismatches in project, lockfile and API versions', () => {
	const f = fixture();
	try {
		assert.equal(packageVersion(f.cwd), version);
		for (const values of [['2.0.0',version,version],[version,'2.0.0',version],[version,version,'2.0.0']]) {
			writeVersions(f.cwd, ...values);
			assert.throws(() => packageVersion(f.cwd), /versions must match/);
		}
	} finally { f.clean(); }
});
test('validate-version checks the release branch without choosing a version', () => {
	assert.match(withOutput(() => validateVersion({GITHUB_REF_NAME:'releases/1.0.0'}, () => version)), /version=1.0.0/);
	assert.throws(() => validateVersion({GITHUB_REF_NAME:'releases/2.0.0'}, () => version), /branch version/);
	assert.throws(() => validateVersion({GITHUB_EVENT_NAME:'pull_request',GITHUB_HEAD_REF:'releases/2.0.0'}, () => version), /branch version/);
});
test('plan-rc requires a validated matching release branch and allowed event', () => {
	const fixturePackage = fixture();
	const env = {
		...baseEnv,
		GITHUB_EVENT_NAME: 'push',
		GITHUB_REF_NAME: `releases/${version}`,
		GITHUB_REF: `refs/heads/releases/${version}`,
		GITHUB_RUN_NUMBER: '19'
	};
	try {
		const result = withOutput(() => planRc(env, () => version));
		assert.match(result, /tag=1\.0\.0-rc\.19/);
		assert.throws(
			() => planRc({ ...env, GITHUB_EVENT_NAME: 'pull_request' }, () => version),
			/RC plan requires/
		);
		assert.throws(
			() => planRc({ ...env, GITHUB_REF: 'refs/heads/releases/1x0x0' }, () => version),
			/RC plan requires/
		);
	} finally {
		fixturePackage.clean();
	}
});
test('plan-final accepts a squash-merged release whose PR head differs from the merge SHA', () => {
	const fixturePackage = fixture();
	const squashPr = { ...pr, head: { ...pr.head, sha: mergeParent } };
	const { runner, calls } = makeRunner({ associatedPrs: [squashPr] });
	try {
		const result = withOutput(() =>
			planFinal(baseEnv, runner, () => packageVersion(fixturePackage.cwd))
		);
		assert.match(result, /eligible=true/);
		assert.ok(
			calls.every(
				([, args]) =>
					!args.includes('repos') || (typeof args[1] === 'string' && args[1].startsWith('repos/'))
			)
		);
		assert.equal(
			calls.some(([, args]) => args[1] === `repos/${repo}/git/commits/${commit}`),
			false,
			'eligibility must not depend on merge-commit parents'
		);
	} finally {
		fixturePackage.clean();
	}
});
test('plan-final rejects PRs that are unmerged, target another base, come from forks, use another branch, or name another merge SHA', () => {
	const fixturePackage = fixture();
	const invalidPrs = [
		{ ...pr, merged_at: null },
		{ ...pr, base: { ...pr.base, ref: 'develop' } },
		{ ...pr, head: { ...pr.head, repo: { full_name: 'fork/repository' } } },
		{ ...pr, head: { ...pr.head, ref: 'feature/release' } },
		{ ...pr, merge_commit_sha: parent }
	];
	try {
		for (const invalidPr of invalidPrs) {
			const { runner } = makeRunner({ associatedPrs: [invalidPr] });
			const result = withOutput(() =>
				planFinal(baseEnv, runner, () => packageVersion(fixturePackage.cwd))
			);
			assert.match(result, /eligible=false/);
		}
		for (const env of [
			{ ...baseEnv, GITHUB_REF: 'refs/heads/releases/1.0.0' },
			{ ...baseEnv, GITHUB_EVENT_NAME: 'pull_request' }
		]) {
			const { runner, calls } = makeRunner();
			const result = withOutput(() => planFinal(env, runner));
			assert.match(result, /eligible=false/);
			assert.equal(
				calls.some(([, args]) => args[1]?.endsWith('/pulls')),
				false
			);
		}
	} finally {
		fixturePackage.clean();
	}
});
test('plan-final skips ordinary main pushes', () => {
	const seen = [];
	const runner = (command, args) => {
		seen.push([command, args]);
		return JSON.stringify({ object: { sha: commit } });
	};
	const result = withOutput(() => planFinal(baseEnv, runner));
	assert.equal(seen[0][1][1], `repos/${repo}/git/ref/heads/main`);
	assert.match(result, /eligible=false/);
});
test('image inspection fetches and validates immutable digest metadata', () => {
	const seen = [];
	const result = checkImage(
		{ image, tag: version, sha: commit, version, repo },
		(command, args) => {
			seen.push(args);
			return args.includes('--raw') ? JSON.stringify(manifest) : `Digest: ${digest}`;
		}
	);
	assert.deepEqual(result, { exists: true, digest });
	assert.equal(seen[1].at(-1), `${image}@${digest}`);
});
test('image status distinguishes missing manifests from authorization failures', () => {
	const input = { image, tag: version, sha: commit, version, repo };
	for (const message of ['manifest unknown', 'no such manifest', 'name unknown']) {
		assert.deepEqual(
			checkImage(input, () => {
				throw Error(message);
			}),
			{ exists: false }
		);
	}
	assert.throws(
		() =>
			checkImage(input, () => {
				throw Error('unauthorized');
			}),
		/unauthorized/
	);
});
test('image inspection recognizes the exact Buildx CI missing-image error', () => {
	const calls = [];
	assert.deepEqual(
		checkImage(
			{
				image: 'ghcr.io/sirily/cashier-server-python',
				tag: '1.1.0-rc.85',
				sha: commit,
				version: '1.1.0-rc.85',
				repo: 'sirily/cashier-server-python'
			},
			(command, args) => {
				calls.push([command, args]);
				throw Error(
					'docker buildx failed: ERROR: ghcr.io/sirily/cashier-server-python:1.1.0-rc.85: not found'
				);
			}
		),
		{ exists: false }
	);
	assert.deepEqual(calls, [
		['docker', ['buildx', 'imagetools', 'inspect', 'ghcr.io/sirily/cashier-server-python:1.1.0-rc.85']]
	]);
});
for (const tag of [`${version}-rc.85`, version]) {
	test(`image status reports exists=false for a missing new image ${tag}`, () => {
		const { runner, calls } = makeRunner();
		const result = withOutput(() =>
			assert.deepEqual(
				imageStatus({ ...baseEnv, RELEASE_TAG: tag, ...(tag.includes('-rc.') ? {
									GITHUB_REF: `refs/heads/releases/${version}`,
									GITHUB_REF_NAME: `releases/${version}`, GITHUB_RUN_NUMBER: '85'
								} : {}) }, (command, args) => {
					if (command === 'docker') {
						assert.deepEqual(args, ['buildx', 'imagetools', 'inspect', `${image}:${tag}`]);
						throw Error(`docker buildx failed: ERROR: ${image}:${tag}: not found`);
					}
					return runner(command, args);
				}),
				{ exists: false }
			)
		);
		assert.equal(result, 'exists=false\n');
		assert.ok(calls.every(([command]) => command === 'gh'));
	});
}
test('Buildx missing final image still blocks an existing final Git tag', () => {
	const { runner } = makeRunner({ tags: [version] });
	assert.equal(
		withOutput(() =>
			assert.throws(
				() =>
					imageStatus(baseEnv, (command, args) => {
						if (command === 'docker')
							throw Error(`docker buildx failed: ERROR: ${image}:${version}: not found`);
						return runner(command, args);
					}),
				/Final image .* is missing for an existing Git tag/
			)
		),
		''
	);
});
test('image inspection does not suppress auth, network, or unrelated not-found errors', () => {
	const ref = `${image}:${version}`;
	for (const message of [
		'unauthorized',
		'404 Not Found',
		'docker buildx failed: ERROR: token endpoint: 404 Not Found',
		`docker buildx failed: ERROR: ${ref}: unauthorized`,
		`docker buildx failed: ERROR: ${ref}: 403 Forbidden`,
		`docker buildx failed: ERROR: ${ref}: 404 Not Found`,
		`docker buildx failed: ERROR: ${ref}: failed to authorize: token not found`,
		`docker buildx failed: ERROR: ${ref}: dial tcp: lookup ghcr.io: no such host`,
		`docker buildx failed: ERROR: ${ref}: net/http: TLS handshake timeout`,
		`docker buildx failed: ERROR: ghcr.io/other/repository:${version}: not found`,
		`docker buildx failed: ERROR: ${image}:${version}-rc.85: not found`,
		`docker buildx failed: ERROR: ${image.replaceAll('.', 'x')}:${version}: not found`,
		'docker buildx failed: ERROR: not found',
		'docker buildx failed: ERROR: manifest unknown: unauthorized',
		`docker buildx failed: ERROR: ${ref}: not found\n403 Forbidden`
	]) {
		const error = Error(message);
		assert.throws(
			() =>
				checkImage({ image, tag: version, sha: commit, version, repo }, () => {
					throw error;
				}),
			(thrown) => thrown === error,
			message
		);
	}
});
test('Buildx not-found errors during raw digest inspection fail closed', () => {
	for (const ref of [`${image}:${version}`, `${image}@${digest}`]) {
		const error = Error(`docker buildx failed: ERROR: ${ref}: not found`);
		const calls = [];
		assert.throws(
			() =>
				checkImage({ image, tag: version, sha: commit, version, repo }, (command, args) => {
					calls.push([command, args]);
					if (args.includes('--raw')) throw error;
					return `Digest: ${digest}`;
				}),
			(thrown) => thrown === error
		);
		assert.equal(calls.length, 2);
		assert.deepEqual(calls[1], [
			'docker',
			['buildx', 'imagetools', 'inspect', '--raw', `${image}@${digest}`]
		]);
	}
});
test('image metadata collision and missing platforms fail closed', () => {
	const input = { image, tag: version, sha: commit, version, repo };
	assert.throws(
		() =>
			checkImage(input, (_, args) =>
				args.includes('--raw')
					? JSON.stringify({ ...manifest, manifests: [] })
					: `Digest: ${digest}`
			),
		/missing required platforms/
	);
	assert.throws(
		() =>
			checkImage({ ...input, sha: parent }, (_, args) =>
				args.includes('--raw') ? JSON.stringify(manifest) : `Digest: ${digest}`
			),
		/tag collision/
	);
});
test('image status checks RC annotation against the full tag', () => {
	const rcTag = `${version}-rc.19`,
		env = { ...baseEnv, RELEASE_TAG: rcTag,
					GITHUB_REF: `refs/heads/releases/${version}`,
					GITHUB_REF_NAME: `releases/${version}`, GITHUB_RUN_NUMBER: '19' };
	const rcManifest = {
		...manifest,
		annotations: { ...manifest.annotations, 'org.opencontainers.image.version': rcTag }
	};
	const { runner } = makeRunner({ manifestValue: rcManifest });
	const result = withOutput(() =>
		assert.deepEqual(imageStatus(env, runner), { exists: true, digest })
	);
	assert.match(result, /exists=true/);
});
test('image status permits an exact final Git-tag retry only with its existing image', () => {
	const { runner } = makeRunner({ tags: [version] });
	assert.deepEqual(imageStatus(baseEnv, runner), { exists: true, digest });
});
test('image status blocks missing final image for an existing tag and rejects a conflicting tag', () => {
	const missing = makeRunner({ tags: [version], imagePresent: false });
	assert.throws(() => imageStatus(baseEnv, missing.runner), /image .* is missing/i);
	const conflict = makeRunner({ tags: [version], tagSha: parent });
	assert.throws(() => imageStatus(baseEnv, conflict.runner), /Refusing existing Git tag/);
	assert.equal(
		conflict.calls.some(([, args]) => args[0] === 'docker'),
		false
	);
});
test('manifest absence is recognized only on the initial tag lookup', () => {
	assert.throws(
		() =>
			checkImage({ image, tag: version, sha: commit, version, repo }, (_, args) => {
				if (!args.includes('--raw')) return `Digest: ${digest}`;
				throw Error('manifest unknown');
			}),
		/manifest unknown/
	);
});
test('finish-final creates a Git tag, release, and latest only for a verified new final', () => {
	const f = fixture(),
		{ runner, calls } = makeRunner();
	try {
		finishFinal(baseEnv, runner, () => packageVersion(f.cwd));
		assert.ok(
			calls.some(
				([cmd, args]) =>
					cmd === 'gh' &&
					args[1] === `repos/${repo}/git/refs` &&
					args.includes(`ref=refs/tags/v${version}`)
			)
		);
		assert.ok(
			calls.some(([cmd, args]) => cmd === 'gh' && args[0] === 'release' && args[1] === 'create')
		);
		assert.ok(calls.some(([cmd, args]) => cmd === 'docker' && args.includes(`${image}:latest`)));
	} finally {
		f.clean();
	}
});
test('finish-final accepts squash merge SHA independently of the release branch head SHA', () => {
	const f = fixture();
	const squashPr = { ...pr, head: { ...pr.head, sha: mergeParent } };
	const { runner, calls } = makeRunner({ associatedPrs: [squashPr] });
	try {
		finishFinal(baseEnv, runner, () => packageVersion(f.cwd));
		assert.ok(calls.some(([cmd, args]) => cmd === 'gh' && args[1] === `repos/${repo}/git/refs`));
	} finally {
		f.clean();
	}
});
test('finish-final permits an exact existing commit tag without recreating it or the release', () => {
	const f = fixture(),
		{ runner, calls } = makeRunner({ tags: [version], releaseExists: true });
	try {
		finishFinal(baseEnv, runner, () => packageVersion(f.cwd));
		assert.equal(
			calls.filter(
				([cmd, args]) =>
					cmd === 'gh' && args[1] === `repos/${repo}/git/refs` && args.includes('POST')
			).length,
			0
		);
		assert.equal(
			calls.filter(([cmd, args]) => cmd === 'gh' && args[0] === 'release' && args[1] === 'create')
				.length,
			0
		);
		assert.ok(calls.some(([cmd, args]) => cmd === 'docker' && args.includes(`${image}:latest`)));
	} finally {
		f.clean();
	}
});
test('finish-final marks new and existing GitHub releases Latest only after verifying container latest', () => {
	for (const releaseExists of [false, true]) {
		const { runner: baseRunner, calls } = makeRunner({
			tags: releaseExists ? [version] : [],
			releaseExists
		});
		const runner = (command, args, options) => {
			if (command === 'gh' && args[0] === 'release' && args[1] === 'edit') {
				assert.equal(options.env.GH_TOKEN, baseEnv.GH_TOKEN);
			}
			return baseRunner(command, args);
		};
		finishFinal(baseEnv, runner, () => version);
		assert.deepEqual(calls.at(-1), [
			'gh',
			['release', 'edit', `v${version}`, '--repo', repo, '--latest']
		]);
		assert.deepEqual(calls.at(-2), [
			'docker',
			['buildx', 'imagetools', 'inspect', '--raw', `${image}@${digest}`]
		]);
		if (!releaseExists) {
			const creation = calls.find(
				([command, args]) => command === 'gh' && args[0] === 'release' && args[1] === 'create'
			);
			assert.ok(creation[1].includes('--latest=false'));
		}
	}
});

test('finish-final does not mark GitHub Latest if container latest publication or verification fails', () => {
	for (const failure of ['push', 'missing', 'mismatch']) {
		const { runner: baseRunner, calls } = makeRunner();
		const runner = (command, args) => {
			if (command === 'docker' && args.includes(`${image}:latest`)) {
				if (args.includes('create') && failure === 'push') throw Error('registry unavailable');
				if (args.includes('inspect')) {
					if (failure === 'missing') throw Error('manifest unknown');
					if (failure === 'mismatch') return `Digest: sha256:${'e'.repeat(64)}`;
				}
			}
			return baseRunner(command, args);
		};
		assert.throws(
			() => finishFinal(baseEnv, runner, () => version),
			/registry unavailable|latest does not point to the final release image/
		);
		assert.equal(
			calls.some(([command, args]) => command === 'gh' && args[1] === 'edit'),
			false
		);
	}
});

test('finish-final surfaces GitHub Latest update failures and permits retrying the existing release', () => {
	const { runner: baseRunner } = makeRunner({ tags: [version], releaseExists: true });
	const runner = (command, args) => {
		if (command === 'gh' && args[0] === 'release' && args[1] === 'edit')
			throw Error('GitHub unavailable');
		return baseRunner(command, args);
	};
	assert.throws(() => finishFinal(baseEnv, runner, () => version), /GitHub unavailable/);
	const retry = makeRunner({ tags: [version], releaseExists: true });
	finishFinal(baseEnv, retry.runner, () => version);
	assert.deepEqual(retry.calls.at(-1), [
		'gh',
		['release', 'edit', `v${version}`, '--repo', repo, '--latest']
	]);
});

test('finish-final rejects a conflicting existing tag, newer version, and moved main without writes', () => {
	const f = fixture();
	try {
		for (const [options, pattern] of [
			[{ tags: [version] }, /existing Git tag/],
			[{ tags: ['1.10.0'] }, /newer final version/],
			[{ moved: true }, /not current main/]
		]) {
			const { runner: baseRunner, calls } = makeRunner(options);
			const runner =
				options.tags?.[0] === version
					? (command, args, opts) => {
							if (command === 'gh' && args[1] === `repos/${repo}/git/ref/tags/v${version}`)
								return JSON.stringify({ object: { type: 'tag', sha: parent } });
							return baseRunner(command, args, opts);
						}
					: baseRunner;
			assert.throws(() => finishFinal(baseEnv, runner, () => packageVersion(f.cwd)), pattern);
			assert.equal(
				calls.some(([cmd, args]) => cmd === 'gh' && args[1] === `repos/${repo}/git/refs`),
				false
			);
		}
	} finally {
		f.clean();
	}
});
test('finish-final rejects wrong package version, image digest, and image tag collisions', () => {
	const f = fixture();
	try {
		const { runner } = makeRunner();
		assert.throws(() => finishFinal(baseEnv, runner, () => '2.0.0'), /package metadata/);
		assert.throws(
			() =>
				finishFinal({ ...baseEnv, IMAGE_DIGEST: 'sha256:bad' }, runner, () =>
					packageVersion(f.cwd)
				),
			/digest/
		);
		assert.throws(
			() => finishFinal({ ...baseEnv, RELEASE_TAG: '2.0.0' }, runner, () => packageVersion(f.cwd)),
			/matching merged release PR/
		);
	} finally {
		f.clean();
	}
});
test('finish-final stops before updating latest if main moves after creating the release', () => {
	const f = fixture(),
		{ runner, calls } = makeRunner({ moveAfterFirstMain: true });
	try {
		assert.throws(
			() => finishFinal(baseEnv, runner, () => packageVersion(f.cwd)),
			/main moved before updating latest/
		);
		assert.ok(
			calls.some(([cmd, args]) => cmd === 'gh' && args[0] === 'release' && args[1] === 'create')
		);
		assert.equal(
			calls.some(([cmd, args]) => cmd === 'docker' && args.includes(`${image}:latest`)),
			false
		);
		assert.equal(
			calls.some(([cmd, args]) => cmd === 'gh' && args[0] === 'release' && args[1] === 'edit'),
			false
		);
	} finally {
		f.clean();
	}
});
test('finish-final fails closed on auth/network image errors and does not publish ordinary main', () => {
	const { runner } = makeRunner();
	assert.throws(
		() =>
			finishFinal(
				baseEnv,
				(command, args) => {
					if (command === 'docker') throw Error('unauthorized');
					return runner(command, args);
				},
				() => version
			),
		/unauthorized/
	);
	assert.throws(
		() => finishFinal({ ...baseEnv, GITHUB_EVENT_NAME: 'schedule' }, runner, () => version),
		/not current main/
	);
});
