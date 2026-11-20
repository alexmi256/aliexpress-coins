#!/usr/bin/env python3
"""Add the two useful views to the project, if they are not there yet.

Board = what is in flight; Table = everything with Area/Priority/Milestone columns. Grouping and
column order are UI settings that the API does not expose, so this creates the layouts and leaves the
grouping to a one-time click.
"""

from __future__ import annotations

import json
import subprocess

OWNER = 'alexmi256'
TITLE = 'aliexpress-coins'


def gql(query: str, variables: dict | None = None) -> dict:
    payload = {'query': query, 'variables': variables or {}}
    result = subprocess.run(
        ['gh', 'api', 'graphql', '--input', '-'],
        input=json.dumps(payload), capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f'graphql failed: {result.stderr[:400]}')
    data = json.loads(result.stdout)
    if 'errors' in data:
        raise SystemExit(f'graphql errors: {data["errors"]}')
    return data['data']


def main() -> None:
    data = gql(
        'query($login: String!) { user(login: $login) { projectsV2(first: 20) '
        '{ nodes { id number title views(first: 20) { nodes { name } } } } } }',
        {'login': OWNER},
    )
    project = next(p for p in data['user']['projectsV2']['nodes'] if p['title'] == TITLE)
    existing = {v['name'] for v in project['views']['nodes']}
    print(f"project #{project['number']}, existing views: {sorted(existing) or 'none'}")

    for name, layout in (('Board', 'BOARD_LAYOUT'), ('Table', 'TABLE_LAYOUT')):
        if name in existing:
            print(f'{name}: already present')
            continue
        gql(
            'mutation($project: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {'
            ' createProjectV2View(input: {projectId: $project, name: $name, layout: $layout}) '
            '{ projectV2View { name } } }',
            {'project': project['id'], 'name': name, 'layout': layout},
        )
        print(f'{name}: created')

    print(f"url: https://github.com/users/{OWNER}/projects/{project['number']}")


if __name__ == '__main__':
    main()
