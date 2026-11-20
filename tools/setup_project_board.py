#!/usr/bin/env python3
"""Create and populate a GitHub Project (v2) for the repo.

What the Project is for: a single queue of work with a board and a table, not a second source of
truth. Labels, sub-issues and `blocked by` edges stay authoritative; the Project mirrors them into
fields so a view can answer "what is ready now" without reading 43 issue bodies.

Idempotent: re-running adds missing items and re-sets field values.
"""

from __future__ import annotations

import json
import subprocess
import sys

OWNER = 'alexmi256'
REPO = 'alexmi256/aliexpress-coins'
TITLE = 'aliexpress-coins'

AREA_BY_LABEL = {
    'area:recon': 'Recon',
    'area:perception': 'Perception',
    'area:control': 'Control',
    'area:tasks': 'Tasks',
    'area:ops': 'Operation',
    'area:delivery': 'Delivery',
}
PRIORITY_BY_LABEL = {'priority:p0': 'P0', 'priority:p1': 'P1', 'priority:p2': 'P2'}


def gql(query: str, **variables: object) -> dict:
    """POST a GraphQL document with JSON variables.

    `gh api graphql -f key=value` sends every value as a string, which fails for `Int!` variables
    (issue numbers). Sending the whole request as JSON keeps numbers as numbers.
    """
    payload = json.dumps({'query': query, 'variables': variables})
    args = ['gh', 'api', 'graphql', '--input', '-']
    result = subprocess.run(args, input=payload, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f'graphql failed: {result.stderr[:600]}')
    payload = json.loads(result.stdout)
    if 'errors' in payload:
        raise SystemExit(f'graphql errors: {payload["errors"]}')
    return payload['data']


def rest(*args: str) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f'gh {" ".join(args)} failed: {result.stderr[:400]}')
    return result.stdout.strip()


def main() -> None:
    owner_id = gql('query($login: String!) { user(login: $login) { id } }', login=OWNER)['user']['id']

    # --- find or create the project
    existing = gql(
        'query($login: String!) { user(login: $login) { projectsV2(first: 20) { nodes { id number title } } } }',
        login=OWNER,
    )['user']['projectsV2']['nodes']
    match = next((p for p in existing if p['title'] == TITLE), None)
    if match:
        project_id, number = match['id'], match['number']
        print(f'project exists: #{number}')
    else:
        created = gql(
            'mutation($owner: ID!, $title: String!) { createProjectV2(input: {ownerId: $owner, title: $title}) '
            '{ projectV2 { id number } } }',
            owner=owner_id,
            title=TITLE,
        )['createProjectV2']['projectV2']
        project_id, number = created['id'], created['number']
        print(f'project created: #{number}')

    project_number = number  # captured before the item loop reuses `number` for issue numbers
    rest('project', 'link', str(project_number), '--owner', OWNER, '--repo', REPO)

    # --- fields
    fields = gql(
        'query($id: ID!) { node(id: $id) { ... on ProjectV2 { fields(first: 30) '
        '{ nodes { ... on ProjectV2FieldCommon { id name } '
        '... on ProjectV2SingleSelectField { id name options { id name } } } } } } }',
        id=project_id,
    )['node']['fields']['nodes']
    by_name = {f['name']: f for f in fields}

    def ensure_select(name: str, options: list[str]) -> dict:
        """Create a single-select field, or return the existing one."""
        if name in by_name:
            return by_name[name]
        mutation = (
            'mutation($project: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {'
            ' createProjectV2Field(input: {projectId: $project, dataType: SINGLE_SELECT, name: $name, '
            'singleSelectOptions: $options}) { projectV2Field { ... on ProjectV2SingleSelectField '
            '{ id name options { id name } } } } }'
        )
        # nested input objects cannot go through `-f`, so send the whole request as JSON
        variables = json.dumps(
            {
                'query': mutation,
                'variables': {
                    'project': project_id,
                    'name': name,
                    'options': [{'name': option, 'color': 'GRAY', 'description': ''} for option in options],
                },
            }
        )
        result = subprocess.run(
            ['gh', 'api', 'graphql', '--input', '-'],
            input=variables, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f'field create failed: {result.stderr[:400]}')
        data = json.loads(result.stdout)
        if 'errors' in data:
            raise SystemExit(f'field create errors: {data["errors"]}')
        return data['data']['createProjectV2Field']['projectV2Field']

    area_field = ensure_select('Area', list(AREA_BY_LABEL.values()))
    prio_field = ensure_select('Priority', ['P0', 'P1', 'P2'])
    status_field = by_name.get('Status')
    if status_field is None:
        raise SystemExit('Status field missing on a new project')
    status_options = {o['name']: o['id'] for o in status_field.get('options', [])}
    area_options = {o['name']: o['id'] for o in area_field['options']}
    prio_options = {o['name']: o['id'] for o in prio_field['options']}

    # --- items
    issues = json.loads(rest('issue', 'list', '-R', REPO, '--state', 'all', '--limit', '100',
                             '--json', 'number,title,state,labels,milestone,url'))
    existing_items = gql(
        'query($id: ID!) { node(id: $id) { ... on ProjectV2 { items(first: 100) '
        '{ nodes { id content { ... on Issue { number } } } } } } }',
        id=project_id,
    )['node']['items']['nodes']
    already = {i['content']['number'] for i in existing_items if i.get('content')}

    added = 0
    for issue in issues:
        issue_number = issue['number']
        if issue_number in already:
            continue
        content_id = gql(
            'query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) '
            '{ issue(number: $number) { id } } }',
            owner='alexmi256', repo='aliexpress-coins', number=issue_number,
        )['repository']['issue']['id']
        gql(
            'mutation($project: ID!, $content: ID!) { addProjectV2ItemById(input: {projectId: $project, '
            'contentId: $content}) { item { id } } }',
            project=project_id, content=content_id,
        )
        added += 1
    print(f'items added: {added} (total issues {len(issues)})')

    # --- field values
    items = gql(
        'query($id: ID!) { node(id: $id) { ... on ProjectV2 { items(first: 100) '
        '{ nodes { id content { ... on Issue { number state labels(first: 12) { nodes { name } } } } } } } } }',
        id=project_id,
    )['node']['items']['nodes']

    by_number = {i['content']['number']: i for i in items if i.get('content')}
    updated = 0
    for issue in issues:
        item = by_number.get(issue['number'])
        if item is None:
            continue
        labels = [label['name'] for label in issue['labels']]
        values: list[tuple[dict, str]] = []
        area = next((AREA_BY_LABEL[name] for name in labels if name in AREA_BY_LABEL), None)
        prio = next((PRIORITY_BY_LABEL[name] for name in labels if name in PRIORITY_BY_LABEL), None)
        if area and area in area_options:
            values.append((area_field, area_options[area]))
        if prio and prio in prio_options:
            values.append((prio_field, prio_options[prio]))
        if issue['state'] == 'CLOSED' and 'Done' in status_options:
            values.append((status_field, status_options['Done']))

        for field, option_id in values:
            gql(
                'mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {'
                ' updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field, '
                'value: {singleSelectOptionId: $option}}) { projectV2Item { id } } }',
                project=project_id, item=item['id'], field=field['id'], option=option_id,
            )
            updated += 1
    print(f'field values set: {updated}')
    print(f'project url: https://github.com/users/{OWNER}/projects/{project_number}')


if __name__ == '__main__':
    sys.exit(main())
