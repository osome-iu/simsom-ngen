<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Git Branch Naming

## Overview

Besides the `main` and `dev` branches, branches should be focused, short-lived, and descriptively named.  Small pull requests make code reviews faster and easier which makes software less buggy, easier to understand, and easier to build.

> [!IMPORTANT]
> This document describes branch naming and conventions for _direct contributions_ to SimSoM NGen.  When _using SimSoM NGen for research or replication_, please [fork this repository](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/fork-a-repo) to your own account.
>
> The branch `main` will have the latest version of SimSoM NGen verified to work.  The branch `dev` will include the latest updates, but may not be as reliable.
>
> When creating branches on your own fork, it's still recommended to use the conventions described here when possible.

### Basic Git Workflow

```bash
# switch to primary dev branch
git switch dev

# update main dev branch
git pull origin dev

# create new branch
git branch feature/new-feature
git switch feature/new-feature

# do work and commit 
git add .
git commit -m "added new feature"

# get latest state of repository
git fetch origin

# update your new working branch with any updates that have been made to dev since you started
git rebase origin/dev

# if there is a merge conflict during rebase:

# fix the merge conflict, commit the update, continue rebasing
git add .
git commit -m "fixed merge conflict"
git rebase --continue

# push the feature branch
git push origin feature/new-feature

# Open Pull Request on Github, targeting the `dev` branch

```

## Branches

### `main` Branch (Production Branch)

There will be one `main` branch that will always contain the production version of the simulator.  The only time you should create a new branch from `main` is when there is a security issue or hotfix that needs to be put into production immediately.  Only hotfix branches and the `dev` branch will merge into `main`.  This branch MUST ALWAYS be runnable.  It's the branch that someone will clone and run their own experiments with.

### `dev` Branch (Integration and Testing Branch)
There will be one `dev` branch which will be the current integration branch where all new work will be integrated.  It's the "frontier" branch.  `dev` should always be runnable, but is not guaranteed to be correct.  This is the branch where we fix merge conflicts, etc. before merging into `main`.  *Merging into main* means that we've decided that it can be pushed into production and it can be used for actual research by people who are not us.

### Feature Branches

A *Feature* is anything that must be implemented but is *not* a bug fix, chore, refactor, documentation change, or research task.  For example, adding a new termination criteria is a feature.  Changing the implementation of the RecSys is a feature.  Adding a new configuration option is a feature.  Optimizing a SQL query is a feature.  A feature branch should be short lived and contain only changes to code and documentation that impact that feature.

### Bug Fix Branches

A *Fix* is anything that is significantly impacting the functionality of an existing feature.  For example, a memory leak in the RecSys implementation causes the simulator to crash.  The Policy system considers friends instead of followers when making a judgement.  User action count is so low that the simulator terminates before convergence every time.  Bug fix branches should only contain changes that fix the bug.  They should not contain any new features, unnecessary refactoring, or unrelated optimizations.

### Chore Branches

*Chores* are routine maintenance tasks that do not change the functionality of the simulator, its tests, or its documentation.  For example, updating dependencies, changing build or development tooling, and maintaining CI configuration are chores.  Chores do not add new features, fix bugs, refactor production code, or update documentation.

### Refactor Branches

A *Refactor* restructures existing code without changing its externally observable behavior.  For example, extracting a shared helper, simplifying control flow, or reorganizing a module without changing its public interface are refactors.  Refactor branches should not add new features, fix bugs, or include unrelated maintenance work.

### Docs Branches

*Docs* are changes limited to documentation.  For example, updating a README, correcting API documentation, or improving code comments are docs changes.  Docs branches should not change the functionality of the simulator or include unrelated code changes.

### Research Branches

*Research* includes analysis and experimentation, primarily via Jupyter notebooks and benchmarking scripts.  Research branches should not contain implementation of new features, bug fixes, or anything not related to the research or calibration being worked on.

## Branch Naming

Branches should be named after the type of work being conducted and the expected outcome or feature being implemented.  Names should be intelligible and easily understood by other developers.  The format of the branch names should be

```
<feature|fix|chore|refactor|docs|research>/<short-stub-describing-work>
```

That is, the type of branch followed by a forward slash followed by a short, kebab-case description stub.

Examples of acceptable branch names:
- `feature/new-policy-implementation`
- `feature/insert-user-sql-optimization`
- `feature/user-authentication`
- `feature/follower-network-homophily`
- `fix/network-generation-hangs`
- `fix/wrong-sample-function`
- `fix/clock-runs-backwards`
- `chore/update-development-dependencies`
- `chore/update-ci-configuration`
- `chore/convert-tabs-to-spaces`
- `refactor/configuration-parser`
- `refactor/extract-policy-helper`
- `docs/update-code-comments`
- `docs/reorganize-documentation-folder`
- `research/calibrate-repost-distribution`
- `research/profile-vectors`
- `research/follower-network-homophily`

Examples of *bad* branch names:
- `frontend`
- `dev-209`
- `homophily`
- `new-branch`
- `new-dev-209-homophily`
- `frontend-backup`

### Adding GitHub Issue Numbers to Branch Names

Branch names should reflect the feature or code-related task that it impacts.  This is not always related to a specific issue.  Some issues may require multiple features, and thus multiple branches.  A branch should only include the code that impacts a specific feature and **should not include all code related to _all_ features, chores, refactors, documentation, research, and fixes described in an issue**.

If you must add the issue number to a branch name, use the following format:

```
<feature|fix|chore|refactor|docs|research>/<issue_number>-<short-stub-describing-work>
```

That is, the type of branch followed by a forward slash followed by the issue number followed by a hyphen followed by a short, kebab-case description stub.

## Merging

### Pull Requests

Pull Requests should ALWAYS target `dev` branch.  Description of Pull Requests should include references to the related issues (if any) and any dependent Pull Requests (see section on *Stacked Branches*).  Pull Requests should never be merged without a review.  Pull Requests can remain open while reviewer comments or merge conflicts are being addressed.  

Additional work that does not address the pull request should not be completed on a branch with an open pull request.

### Rebase Before a Pull Requests

Since there are multiple people working on multiple branches, it is likely that the `dev` branch will have changed somewhat before you are ready to merge.  To limit the number of merge conflicts that need to be addressed during the PR/Code Review, always rebase or merge the `dev` branch into your working branch.  If there are merge conflicts, they can be addressed here, before a pull request is opened.

### Deleting Branches

Branches should always be deleted immediately after a pull request is merged.  You should never work on a branch that has been deleted after a merge.  This can lead to significant conflicts and extra work.  Always create a new branch for a new task.

### Code Review Size

The size of a pull request should be limited.  The lines of code changed should be minimized and kept to only those changes required for the intended task to be completed.  A smaller pull request diff size results in a faster and easier code review.  Faster code reviews over a smaller diff result in fewer bugs and fewer changes later.

## Stacked Branches / Pull Requests

> *Note:*  This is an optional enhancement to a developer's workflow.  You don't have to use it as long as we're keeping code review size reasonable.  This is only if you want to continue working without waiting for a code review.  **You are not required to use stacked pull requests.**

> This is a lightweight version of the "stacked pull request" methodology.  It does not rely on GitHub's *Stacked Pull Request* feature or additional tooling.  It relies on discipline, communication, and reasonable code reviews.

There are instances where you'll complete work on a branch, create a PR for that, and need to continue work before that branch has been reviewed and merged into `dev`.  In this case, create a new branch from the branch that you've just opened a pull request for.  This child branch is known as a *stacked branch*.  The stacked branch should be named according to the normal branch naming rules.  There's nothing particularly special about a stacked branch except that it's not branched directly from `dev`.

### Opening a Stacked Pull Request

If the parent PR is not reviewed and merged by the time you're ready to merge the stacked branch, add a link to the previous PR in the description of the new PR, indicating that the previous PR must be completed first.  When a PR has a dependency like this, it's known as a *stacked pull request*.

When opening a stacked pull request, you must still *target the `dev` branch*.  **Do not target the parent branch**.

### Stacked PR Benefits

The goal of stacked PRs is to keep the PR diffs small, allowing for quicker code reviews and quicker merges.  This will seem counter intuitive when you look at the diff of the stacked PR and see that it's twice as big as the parent PR.  This happens because the stacked PR contains all the changes of both parent branch AND the stacked branch.

When the parent pull request is merged into `dev`, `dev` will now have all the changes made by the parent branch.  Those changes will magically disappear from the staked PR's diff when the parent is merged into `dev` because *both `dev` and the stacked branch will have all of the changes made by the parent branch*, so the PR will no longer see the parent's changes as a difference.

Every time a branch in the stack is merged, the size of subsequent diffs will shrink, keeping the code reviews small and fast.

This also reduces (or eliminates) the "down time" wasted while waiting for a PR to be completed.  You can continue your development work without relying on a code review to happen first.  PRs can be stacked infinitely (though you should try to keep your stacks small... see drawbacks below).

### Stacked PR Drawbacks

Stacked PRs must be completed in the correct order.  You must not try to complete a stacked PR before a parent PR has been completed.  Later PRs will have much larger diffs to review, which will take longer and result in more changes that need to be made all at once.  Reviewing and merging PRs in order keeps the reviews small and fast so that there are fewer merge conflicts to take care of at once.

Larger stacks could still result in more merge conflicts even though the reviews will be smaller.  If a review comment is addressed in an earlier PR, the changes created to address that comment will be merged into `dev` while all the other branches higher in the stack will not contain those fixes.  This can be mitigated by merging `dev` back into the stacked branch before starting the review, but that could trigger merge conflicts before the review even starts.  These conflicts could continue bubbling up the stack.

My recommendation is to *limit a stack to less than 4 PRs*.  When you've reached a point where you feel the working branch has diverged too much from `dev` (when the stack is too big), take a break and start poking people to get started on the code reviews.
