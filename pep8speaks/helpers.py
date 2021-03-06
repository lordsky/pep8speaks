# -*- coding: utf-8 -*-

import base64
import datetime
import json
import os
import re
import subprocess
import time

import psycopg2
import unidiff
import yaml
from pep8speaks import utils


def update_users(repository):
    """Update users of the integration in the database"""
    if os.environ.get("OVER_HEROKU", False) is not False:
        # Check if repository exists in database
        query = r"INSERT INTO Users (repository, created_at) VALUES ('{}', now());" \
                "".format(repository)

        # cursor and conn are bultins, defined in app.py
        try:
            cursor.execute(query)
            conn.commit()
        except psycopg2.IntegrityError:  # If already exists
            conn.rollback()


def follow_user(user):
    """Follow the user of the service"""
    headers = {
        "Content-Length": "0",
    }
    query = "/user/following/{}".format(user)
    return utils.query_request(query=query, method='PUT', headers=headers)


def get_config(repo, base_branch):
    """
    Get .pep8speaks.yml config file from the repository and return
    the config dictionary
    """

    # Default configuration parameters
    default_config_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'default_config.json')
    with open(default_config_path) as config_file:
        config = json.loads(config_file.read())

    # Configuration file
    query = "https://raw.githubusercontent.com/{}/{}/.pep8speaks.yml"
    query = query.format(repo, base_branch)

    r = utils.query_request(query)

    if r.status_code == 200:
        try:
            new_config = yaml.load(r.text)
            # overloading the default configuration with the one specified
            config = utils.update_dict(config, new_config)
        except yaml.YAMLError:  # Bad YAML file
            pass

    # Create pycodestyle command line arguments
    arguments = []
    confs = config["pycodestyle"]
    for key, value in confs.items():
        if value:  # Non empty
            if isinstance(value, int):
                if isinstance(value, bool):
                    arguments.append("--{}".format(key))
                else:
                    arguments.append("--{}={}".format(key, value))
            elif isinstance(value, list):
                arguments.append("--{}={}".format(key, ','.join(value)))
    config["pycodestyle_cmd_config"] = ' {arguments}'.format(arguments=' '.join(arguments))

    # pycodestyle is case-sensitive
    config["pycodestyle"]["ignore"] = [e.upper() for e in list(config["pycodestyle"]["ignore"])]

    return config


def get_files_involved_in_pr(repo, pr_number):
    """
    Return a list of file names modified/added in the PR
    """
    headers = {"Accept": "application/vnd.github.VERSION.diff"}

    query = "/repos/{}/pulls/{}"
    query = query.format(repo, pr_number)
    r = utils.query_request(query, headers=headers)

    patch = unidiff.PatchSet(r.content.splitlines(), encoding=r.encoding)

    files = {}

    for patchset in patch:
        diff_file = patchset.target_file[1:]
        files[diff_file] = []
        for hunk in patchset:
            for line in hunk.target_lines():
                if line.is_added:
                    files[diff_file].append(line.target_line_no)
    return files


def get_py_files_in_pr(repo, pr_number, exclude=None):
    if exclude is None:
        exclude = []
    files = get_files_involved_in_pr(repo, pr_number)
    for diff_file in list(files.keys()):
        if diff_file[-3:] != ".py" or utils.filename_match(diff_file, exclude):
            del files[diff_file]

    return files


def check_pythonic_pr(repo, pr_number):
    """
    Return True if the PR contains at least one Python file
    """
    return len(get_py_files_in_pr(repo, pr_number)) > 0


def run_pycodestyle(ghrequest, config):
    """
    Runs the pycodestyle cli tool on the files and update ghrequest
    """
    repo = ghrequest.repository
    pr_number = ghrequest.pr_number
    commit = ghrequest.after_commit_hash

    # Run pycodestyle
    ## All the python files with additions
    # A dictionary with filename paired with list of new line numbers
    files_to_exclude = config["pycodestyle"]["exclude"]
    py_files = get_py_files_in_pr(repo, pr_number, files_to_exclude)

    ghrequest.links = {}  # UI Link of each updated file in the PR
    for py_file in py_files:
        filename = py_file[1:]
        query = "https://raw.githubusercontent.com/{}/{}/{}"
        query = query.format(repo, commit, py_file)
        r = utils.query_request(query)
        with open("file_to_check.py", 'w+', encoding=r.encoding) as file_to_check:
            file_to_check.write(r.text)

        # Use the command line here
        cmd = 'pycodestyle {config[pycodestyle_cmd_config]} file_to_check.py'.format(
            config=config)
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        stdout, _ = proc.communicate()
        ghrequest.extra_results[filename] = stdout.decode(r.encoding).splitlines()

        # Put only relevant errors in the ghrequest.results dictionary
        ghrequest.results[filename] = []
        for error in list(ghrequest.extra_results[filename]):
            if re.search("^file_to_check.py:\d+:\d+:\s[WE]\d+\s.*", error):
                ghrequest.results[filename].append(error.replace("file_to_check.py", filename))
                ghrequest.extra_results[filename].remove(error)

        ## Remove errors in case of diff_only = True
        ## which are caused in the whole file
        for error in list(ghrequest.results[filename]):
            if config["scanner"]["diff_only"]:
                if not int(error.split(":")[1]) in py_files[py_file]:
                    ghrequest.results[filename].remove(error)

        ## Store the link to the file
        url = "https://github.com/{}/blob/{}{}"
        ghrequest.links[filename + "_link"] = url.format(repo, commit, py_file)
        os.remove("file_to_check.py")


def prepare_comment(ghrequest, config):
    """Construct the string of comment i.e. its header, body and footer"""
    author = ghrequest.author
    # Write the comment body
    ## Header
    comment_header = ""
    if ghrequest.action == "opened":
        if config["message"]["opened"]["header"] == "":
            comment_header = "Hello @" + author + "! Thanks for submitting the PR.\n\n"
        else:
            comment_header = config["message"]["opened"]["header"] + "\n\n"
    elif ghrequest.action in ["synchronize", "reopened"]:
        if config["message"]["updated"]["header"] == "":
            comment_header = "Hello @" + author + "! Thanks for updating the PR.\n\n"
        else:
            comment_header = config["message"]["updated"]["header"] + "\n\n"

    ## Body
    ERROR = False  # Set to True when any pep8 error exists
    comment_body = []
    for gh_file, issues in ghrequest.results.items():
        if len(issues) == 0:
            if not config["only_mention_files_with_errors"]:
                comment_body.append(
                    " - There are no PEP8 issues in the"
                    " file [`{0}`]({1}) !".format(gh_file, ghrequest.links[gh_file + "_link"]))
        else:
            ERROR = True
            comment_body.append(
                " - In the file [`{0}`]({1}), following "
                "are the PEP8 issues :\n".format(gh_file, ghrequest.links[gh_file + "_link"]))
            if config["descending_issues_order"]:
                issues = issues[::-1]

            for issue in issues:
                ## Replace filename with L
                error_string = issue.replace(gh_file + ":", "Line ")

                ## Link error codes to search query
                error_string_list = error_string.split(" ")
                code = error_string_list[2]
                code_url = "https://duckduckgo.com/?q=pep8%20{0}".format(code)
                error_string_list[2] = "[{0}]({1})".format(code, code_url)

                ## Link line numbers in the file
                line, col = error_string_list[1][:-1].split(":")
                line_url = ghrequest.links[gh_file + "_link"] + "#L" + line
                error_string_list[1] = "[{0}:{1}]({2}):".format(line, col, line_url)
                error_string = " ".join(error_string_list)
                error_string = error_string.replace("Line [", "[Line ")
                comment_body.append("\n> {0}".format(error_string))

        comment_body.append("\n\n")
        if len(ghrequest.extra_results[gh_file]) > 0:
            comment_body.append(" - Complete extra results for this file :\n\n")
            comment_body.append("> " + "".join(ghrequest.extra_results[gh_file]))
            comment_body.append("---\n\n")

    if config["only_mention_files_with_errors"] and not ERROR:
        comment_body.append(config["message"]["no_errors"])

    comment_body = ''.join(comment_body)

    ## Footer
    comment_footer = []
    if ghrequest.action == "opened":
        comment_footer.append(config["message"]["opened"]["footer"])
    elif ghrequest.action in ["synchronize", "reopened"]:
        comment_footer.append(config["message"]["updated"]["footer"])

    comment_footer = ''.join(comment_footer)

    return comment_header, comment_body, comment_footer, ERROR


def comment_permission_check(ghrequest):
    """
    Check for quite and resume status or duplicate comments
    """
    repository = ghrequest.repository

    # Check for duplicate comment
    url = "/repos/{}/issues/{}/comments"
    url = url.format(repository, str(ghrequest.pr_number))
    comments = utils.query_request(url).json()

    # # Get the last comment by the bot
    # last_comment = ""
    # for old_comment in reversed(comments):
    #     if old_comment["user"]["id"] == 24736507:  # ID of @pep8speaks
    #         last_comment = old_comment["body"]
    #         break

    # # Disabling this because only a single comment is made per PR
    # text1 = ''.join(BeautifulSoup(markdown(comment)).findAll(text=True))
    # text2 = ''.join(BeautifulSoup(markdown(last_comment)).findAll(text=True))
    # if text1 == text2.replace("submitting", "updating"):
    #     PERMITTED_TO_COMMENT = False

    # Check if the bot is asked to keep quiet
    for old_comment in reversed(comments):
        if '@pep8speaks' in old_comment['body']:
            if 'resume' in old_comment['body'].lower():
                break
            elif 'quiet' in old_comment['body'].lower():
                return False

    # Check for [skip pep8]
    ## In commits
    commits = utils.query_request(ghrequest.commits_url).json()
    for commit in commits:
        if any(m in commit["commit"]["message"].lower() for m in ["[skip pep8]", "[pep8 skip]"]):
            return False
    ## PR title
    if any(m in ghrequest.pr_title.lower() for m in ["[skip pep8]", "[pep8 skip]"]):
        return False
    ## PR description
    if any(m in ghrequest.pr_desc.lower() for m in ["[skip pep8]", "[pep8 skip]"]):
        return False

    return True


def create_or_update_comment(ghrequest, comment, ONLY_UPDATE_COMMENT_BUT_NOT_CREATE):
    query = "/repos/{}/issues/{}/comments"
    query = query.format(ghrequest.repository, str(ghrequest.pr_number))
    comments = utils.query_request(query).json()

    # Get the last comment id by the bot
    last_comment_id = None
    for old_comment in comments:
        if old_comment["user"]["id"] == 24736507:  # ID of @pep8speaks
            last_comment_id = old_comment["id"]
            break

    if last_comment_id is None and not ONLY_UPDATE_COMMENT_BUT_NOT_CREATE:  # Create a new comment
        response = utils.query_request(query=query, method='POST', json={"body": comment})
        ghrequest.comment_response = response.json()
    else:  # Update the last comment
        utc_time = datetime.datetime.utcnow()
        time_now = utc_time.strftime("%B %d, %Y at %H:%M Hours UTC")
        comment += "\n\n##### Comment last updated on {}"
        comment = comment.format(time_now)

        query = "/repos/{}/issues/comments/{}"
        query = query.format(ghrequest.repository, str(last_comment_id))
        response = utils.query_request(query, method='PATCH', json={"body": comment})

    return response


def autopep8(ghrequest, config):
    # Run pycodestyle

    r = utils.query_request(ghrequest.diff_url)
    ## All the python files with additions
    patch = unidiff.PatchSet(r.content.splitlines(), encoding=r.encoding)

    # A dictionary with filename paired with list of new line numbers
    py_files = {}

    for patchset in patch:
        if patchset.target_file[-3:] == '.py':
            py_file = patchset.target_file[1:]
            py_files[py_file] = []
            for hunk in patchset:
                for line in hunk.target_lines():
                    if line.is_added:
                        py_files[py_file].append(line.target_line_no)

    # Ignore errors and warnings specified in the config file
    to_ignore = ",".join(config["pycodestyle"]["ignore"])
    arg_to_ignore = ""
    if len(to_ignore) > 0:
        arg_to_ignore = "--ignore " + to_ignore

    for py_file in py_files:
        filename = py_file[1:]
        url = "https://raw.githubusercontent.com/{}/{}/{}"
        url = url.format(ghrequest.repository, ghrequest.sha, py_file)
        r = utils.query_request(url)
        with open("file_to_fix.py", 'w+', encoding=r.encoding) as file_to_fix:
            file_to_fix.write(r.text)

        cmd = 'autopep8 file_to_fix.py --diff {arg_to_ignore}'.format(
            arg_to_ignore=arg_to_ignore)
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        stdout, _ = proc.communicate()
        ghrequest.diff[filename] = stdout.decode(r.encoding)

        # Fix the errors
        ghrequest.diff[filename] = ghrequest.diff[filename].replace("file_to_check.py", filename)
        ghrequest.diff[filename] = ghrequest.diff[filename].replace("\\", "\\\\")

        ## Store the link to the file
        url = "https://github.com/{}/blob/{}{}"
        ghrequest.links = {}
        ghrequest.links[filename + "_link"] = url.format(ghrequest.repository, ghrequest.sha, py_file)
        os.remove("file_to_fix.py")


def create_gist(ghrequest):
    """Create gists for diff files"""
    request_json = {}
    request_json["public"] = True
    request_json["files"] = {}
    request_json["description"] = "In response to @{0}'s comment : {1}".format(
        ghrequest.reviewer, ghrequest.review_url)

    for diff_file, diffs in ghrequest.diff.items():
        if len(diffs) != 0:
            request_json["files"][diff_file.split("/")[-1] + ".diff"] = {
                "content": diffs
            }

    # Call github api to create the gist
    query = "/gists"
    response = utils.query_request(query, method='POST', json=request_json).json()
    ghrequest.gist_response = response
    ghrequest.gist_url = response["html_url"]


def delete_if_forked(ghrequest):
    FORKED = False
    query = "/user/repos"
    r = utils.query_request(query)
    for repo in r.json():
        if repo["description"]:
            if ghrequest.target_repo_fullname in repo["description"]:
                FORKED = True
                url = "/repos/{}"
                url = url.format(repo["full_name"])
                utils.query_request(url, method='DELETE')
    return FORKED


def fork_for_pr(ghrequest):
    query = "/repos/{}/forks"
    query = query.format(ghrequest.target_repo_fullname)
    r = utils.query_request(query, method='POST')

    if r.status_code == 202:
        ghrequest.fork_fullname = r.json()["full_name"]
        return True

    ghrequest.error = "Unable to fork"
    return False


def update_fork_desc(ghrequest):
    # Check if forked (takes time)
    query = "/repos/{}".format(ghrequest.fork_fullname)
    r = utils.query_request(query)
    ATTEMPT = 0
    while(r.status_code != 200):
        time.sleep(5)
        r = utils.query_request(query)
        ATTEMPT += 1
        if ATTEMPT > 10:
            ghrequest.error = "Forking is taking more than usual time"
            break

    full_name = ghrequest.target_repo_fullname
    author, name = full_name.split("/")
    request_json = {
        "name": name,
        "description": "Forked from @{}'s {}".format(author, full_name)
    }
    r = utils.query_request(query, method='PATCH', data=json.dumps(request_json))
    if r.status_code != 200:
        ghrequest.error = "Could not update description of the fork"


def create_new_branch(ghrequest):
    query = "/repos/{}/git/refs/heads"
    query = query.format(ghrequest.fork_fullname)
    sha = None
    r = utils.query_request(query)
    for ref in r.json():
        if ref["ref"].split("/")[-1] == ghrequest.target_repo_branch:
            sha = ref["object"]["sha"]

    query = "/repos/{}/git/refs"
    query = query.format(ghrequest.fork_fullname)
    ghrequest.new_branch = "{}-pep8-patch".format(ghrequest.target_repo_branch)
    request_json = {
        "ref": "refs/heads/{}".format(ghrequest.new_branch),
        "sha": sha,
    }
    r = utils.query_request(query, method='POST', json=request_json)

    if r.status_code > 299:
        ghrequest.error = "Could not create new branch in the fork"


def autopep8ify(ghrequest, config):
    # Run pycodestyle
    r = utils.query_request(ghrequest.diff_url)

    ## All the python files with additions
    patch = unidiff.PatchSet(r.content.splitlines(), encoding=r.encoding)

    # A dictionary with filename paired with list of new line numbers
    py_files = {}

    for patchset in patch:
        if patchset.target_file[-3:] == '.py':
            py_file = patchset.target_file[1:]
            py_files[py_file] = []
            for hunk in patchset:
                for line in hunk.target_lines():
                    if line.is_added:
                        py_files[py_file].append(line.target_line_no)

    # Ignore errors and warnings specified in the config file
    to_ignore = ",".join(config["pycodestyle"]["ignore"])
    arg_to_ignore = ""
    if len(to_ignore) > 0:
        arg_to_ignore = "--ignore " + to_ignore

    for py_file in py_files:
        filename = py_file[1:]
        query = "https://raw.githubusercontent.com/{}/{}/{}"
        query = query.format(ghrequest.repository, ghrequest.sha, py_file)
        r = utils.query_request(query)
        with open("file_to_fix.py", 'w+', encoding=r.encoding) as file_to_fix:
            file_to_fix.write(r.text)

        cmd = 'autopep8 file_to_fix.py {arg_to_ignore}'.format(
            arg_to_ignore=arg_to_ignore)
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        stdout, _ = proc.communicate()
        ghrequest.results[filename] = stdout.decode(r.encoding)

        os.remove("file_to_fix.py")


def commit(ghrequest):
    fullname = ghrequest.fork_fullname

    for old_file, new_file in ghrequest.results.items():
        query = "/repos/{}/contents/{}"
        query = query.format(fullname, old_file)
        params = {"ref": ghrequest.new_branch}
        r = utils.query_request(query, params=params)
        sha_blob = r.json().get("sha")
        params["path"] = old_file
        content_code = base64.b64encode(new_file.encode()).decode("utf-8")
        request_json = {
            "path": old_file,
            "message": "Fix pep8 errors in {}".format(old_file),
            "content": content_code,
            "sha": sha_blob,
            "branch": ghrequest.new_branch,
        }
        r = utils.query_request(query, method='PUT', json=request_json)


def create_pr(ghrequest):
    query = "/repos/{}/pulls"
    query = query.format(ghrequest.target_repo_fullname)
    request_json = {
        "title": "Fix pep8 errors",
        "head": "pep8speaks:{}".format(ghrequest.new_branch),
        "base": ghrequest.target_repo_branch,
        "body": "The changes are suggested by autopep8",
    }
    r = utils.query_request(query, method='POST', json=request_json)
    if r.status_code == 201:
        ghrequest.pr_url = r.json()["html_url"]
    else:
        ghrequest.error = "Pull request could not be created"
