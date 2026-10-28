<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Documentation Standards

> [!IMPORTANT]
> - Using AI coding assistance is okay, but all documentation should be written by hand.

- Centralized `docs` folder should not contain code.  It should contain:
  - Architecture descriptions and diagrams
  - High-level system overview
  - High-level algorithms and workflows
  - Schemas
  - API descriptions
- Prose in `docs` should be in markdown format.
  - For formatting options, see https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax
  - Please avoid hard-wrapping (inserting a newline in the middle of a sentence at an arbitrary character count to format a document) in markdown files.
    - A sentence should run as long as it needs to without a manually added line break.
    - Hard-wrapping creates noisy git diffs. Changing one word in a document may cause text to reflow and modify several lines.  These show up as separate line changes in a diff, making it look like dozens of lines have been modified, rather than one sentence in a documentation file, compounding with each change.  If this happens multiple times in one commit, you've suddenly created a 300 line code review.
    - Most people view markdown files in editors or viewers (GitHub, IDEs, `cat`, etc.) that automatically handle reflowing/soft-wrapping.  Using hard-wrapping in a document will often cause rendering issues in editors that support soft-wrapping.


> [!IMPORTANT]
> _**Code Documentation Goal**_
>
> When looking at a function in isolation, the reader should be able to understand what it's for and what it does.

- Implementation details should be described in the file where it is implemented.
  - If the code is implementing an algorithm described in the `docs` folder, the file and section where that algorithm is described should be written in the comments that contain implementation details.
  - Every function must have a docstring (or that language's equivalent).  Each docstring must include:
    - Description of function's purpose
      - Why does it exist?
      - Where is it used?
      - What does it do?
      - Does it implement a specific architecture decision/algorithm (with link to related `docs` file).
    - Description of each input parameter
      - parameter name
      - parameter type (also, where is the type defined, if not in the current file?)
      - what the parameter represents (what _is_ it? What should it contain?)
      - purpose of parameter (why is it here)
    - Description of the return value
      - type (also, where is the type defined, if not in the current file?)
      - what the value represents (what _is_ it? What should it contain?)
      - purpose (why is being returned? What is the calling function supposed to do with it?)
    - Description of each failure mode
      - Exception type thrown
      - What would cause the exception to be thrown?
    - Example of usage
  - Documentation does not excuse ambiguity.
    - variable names must be named descriptively, based on its purpose and/or expected contents
    - Type hinting should be used regardless of documentation.
      - Type hinting is for IDEs and tools
      - Documentation is for humans (and LLMs)
    - Abbreviations must be spelled out after the first use in a function
      - This means it could be written out multiple times in the same file
  - Opaque, hidden, complicated, or difficult to understand data structures, etc. must include a representative example.
    - You want to be able to get an idea of what the contents of the variable looks like without having to do a `print(jsonify(some_random_data_structure))` to understand it.
    - For example, if the code is serializing a deeply nested dictionary, an example of the serialized string must be included as a comment.
    - For example, if the function is building an array of SQL INSERT fragments, an example with two or three items in the array could be included in a comment.
  - Inline comments can be helpful and should be used for complex loops, etc.
    - If the goal is to implement an algorithm, note which part of the algorithm is being implemented in the next line of code.
- Class definitions must adhere to the above standards.  
  - Member functions / Methods should follow the same conventions as functions.
    - The exception is that the `self` parameter does not need to be documented because it is always the same and always required.
  - Class docstring should include:
    - Description (what is it?)
    - Purpose (why is it needed?)
    - Usage (where and how is it used)
    - List of properties with explanations
      - property name
      - property type
      - what does it represent (what is it supposed to contain?)
      - purpose (why do we need it?)
    - Side effects
      - What else in the system does this class impact/change that's not defined as a property?
        - For example, does this class modify the schema of a database?
        - For example, does this class add or delete data from a database table?
        - For example, does this class terminate worker processes?

