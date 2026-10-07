# Geography and insurer join policy

Updated: 2026-10-07

## Current state

The June 2026 MHLW Long-term Care Insurance Business Status Report has been source-qualified for:

- category-1 insured population
- age 75+ population within category-1 insured persons
- category-1 certified persons
- national certification-rate reconstruction

Both insurer-level workbooks yielded 1,574 unique `prefecture + insurer_name` rows, and the two key sets matched exactly. The extracted totals also matched the national rows in the workbooks.

## Join boundary

The qualified monthly workbook columns do not contain an official insurer code.

Therefore `prefecture + insurer_name` is permitted only for validating and joining tables from the same monthly report. It must not be used as the canonical key to join:

-介護サービス情報公表システムの市区町村コード
- population mesh data
- other municipality-level datasets

## Required before municipality publication

Acquire and qualify an official mapping that can represent:

- insurer identifier
- insurer name
- member municipality code(s)
- effective period
- one-to-one and one-to-many relationships
- changes caused by municipal mergers or insurer reorganization

Until that mapping is qualified, municipality-level demand/supply ratios remain blocked.

## Reason

An insurer is not always identical to a single municipality. A name-based join would hide joint insurers and historical changes, and could silently assign a valid demand denominator to the wrong geography.
