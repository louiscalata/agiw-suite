import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';

const res = new URL('./android/app/src/main/res/font/', import.meta.url);
const license = new URL('./android/app/src/main/assets/licenses/Inter-OFL-1.1.txt', import.meta.url);

test('the Inter fonts in the Android app ship with their copyright line and the SIL OFL 1.1 text', () => {
  const fonts = readdirSync(res).filter(name => name.endsWith('.ttf'));
  assert.ok(fonts.length >= 2, fonts.join());
  assert.ok(existsSync(license), 'assets/licenses/Inter-OFL-1.1.txt is missing');
  // A Windows autocrlf checkout rewrites the OFL file to CR LF. The sentence is still
  // one line break, and the assertions below stay on the LF form of that text.
  const text = readFileSync(license, 'utf8').replace(/\r\n/g, '\n');
  assert.match(text, /^Copyright 2016 The Inter Project Authors \(https:\/\/github\.com\/rsms\/inter\)/);
  assert.match(text, /SIL OPEN FONT LICENSE Version 1\.1 - 26 February 2007/);
  for (const clause of ['PREAMBLE', 'DEFINITIONS', 'PERMISSION & CONDITIONS', 'TERMINATION', 'DISCLAIMER',
    'each copy\ncontains the above copyright notice and this license']) assert.ok(text.includes(clause), clause);
  assert.ok(text.length > 4000 && text.length < 6000, String(text.length));
});

const gradle = readFileSync(new URL('./android/app/build.gradle.kts', import.meta.url), 'utf8');
const readme = readFileSync(new URL('./android/README.md', import.meta.url), 'utf8');

test('Gradle packs the licence: it sits in the default assets folder and nothing in the build file moves or filters assets', () => {
  assert.match(license.pathname, /\/android\/app\/src\/main\/assets\/licenses\/Inter-OFL-1\.1\.txt$/);
  assert.doesNotMatch(gradle, /sourceSets|assets\.srcDir|srcDirs|ignoreAssetsPattern|aaptOptions|androidResources|packaging|excludes?\s*\+?=|exclude\(/);
  assert.match(readme, /assets\/licenses\/Inter-OFL-1\.1\.txt/);
});

test('the build-tools version the README tells people to install is the one the build asks for', () => {
  const pinned = gradle.match(/buildToolsVersion\s*=\s*"(\d+\.\d+\.\d+)"/);
  assert.ok(pinned, 'app/build.gradle.kts must pin buildToolsVersion, or the Android Gradle Plugin asks for its own default');
  assert.match(readme, new RegExp(`build-tools;${pinned[1].replace(/\./g, '\\.')}`));
  assert.match(gradle, new RegExp(`compileSdk\\s*=\\s*${readme.match(/platforms;android-(\d+)/)[1]}`));
});
