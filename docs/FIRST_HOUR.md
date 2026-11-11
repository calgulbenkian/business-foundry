# Your first hour with Foundry

This guide is for someone starting or buying a business who rarely uses a terminal. It takes you from nothing installed to your own business workspace.

## What Foundry is, and what it is not

Foundry is a private workspace on your own computer for the work of starting, buying and running a business: customers, offers, decisions, evidence, money and the steps still to do. It helps you prepare and track that work, and it asks a person to review each important step. It does not file with governments, open bank accounts, sign agreements or move money; you do those with the relevant people and providers, then record the receipts in Foundry. It does not certify legal compliance or replace legal, tax, accounting or investment judgment.

This guide was checked on macOS. The project's automated checks run on Ubuntu Linux. Windows and other systems have not been tried.

## What you need

- **A terminal.** This is a window where you type commands and press Enter. On a Mac, open the **Terminal** app (in Applications, then Utilities). On Linux, look for Terminal among your applications.
- **uv**, a free tool that installs and runs Python programs. Follow [uv's installation page](https://docs.astral.sh/uv/getting-started/installation/). If the terminal later says it cannot find `uv`, close the terminal window and open a new one. You do not need to install Python yourself: if your computer lacks Python 3.11 or newer, uv downloads it.
- **An internet connection for the first start**, when uv downloads what Foundry needs. After that, Foundry runs without one.
- **The Foundry code.** Choose one way:
  - With git, type `git clone https://github.com/calgulbenkian/business-foundry.git` in the terminal.
  - Without git, open https://github.com/calgulbenkian/business-foundry in your web browser, choose **Code**, then **Download ZIP**, and unzip the file.

## Start Foundry and sign in

1. In the terminal, move into the Foundry folder with `cd` ("change directory") and the folder's name. For a git copy that is `cd business-foundry`. For a ZIP copy, type `cd ` (with a space) and drag the unzipped folder onto the terminal window, then press Enter.
2. Type `uv run foundry start` and press Enter.

The first time, uv prints a few lines while it sets up (for example "Creating virtual environment" and "Installed … packages"). Then Foundry prints:

```text
Created a new private workspace. Its sign-in key is saved in …/business-foundry/.foundry/owner.key
Added the example business 'Polymer Geospatial · demo'. It is synthetic: its customers, figures and receipts are made up.

Business Foundry is running.

  Open this address in your web browser: http://127.0.0.1:8765
  Sign-in key: bf_…

Keep the key private. Anyone who has it can sign in as the owner of this workspace.
To stop Foundry, press Ctrl+C in this window.
```

Your web browser then opens at **http://127.0.0.1:8765**. If it does not, open your browser and type that address yourself. `127.0.0.1` means "this computer": only programs on your own computer can reach this address.

The page says **Welcome to the Foundry.** Copy the whole sign-in key from the terminal (it starts with `bf_`), paste it into **Access key**, and choose **Open workspace**. Leave the terminal window open while you work; Foundry runs inside it.

If the terminal instead says the port is already in use, Foundry may already be running in another terminal window. Open http://127.0.0.1:8765 in your browser, or start on another port with `uv run foundry start --port 8766`.

## Ten minutes in the example business

You start in **Polymer Geospatial · demo**. The **SYNTHETIC DEMO** banner is a reminder that its customers, figures and receipts are invented. Try any button; unless you set up the optional AI features, nothing in Foundry sends your records anywhere. The list on the left has these screens:

- **Overview**: totals such as **Work verified** and **Cash collected**, and **Your next useful steps**. Choose **Open** beside a step.
- **Work & decisions**: every step, grouped by topic, with its current state. Choose **Review** on a step to see its description, its prerequisites, **Next state**, **Evidence for this decision** and **Decision note**. **Record decision** saves your choice.
- **Business records**: what you know or assume about customers, offers, assets, contracts, roles and more. Each record shows its evidence status, for example **Reported**. **+ Add record** adds one.
- **Evidence**: the original text behind the records and decisions. **Open** shows it; an owner or reviewer can **Record human review**.
- **Financial model**: monthly income, cash and balance sheet under **Base**, **Downside** and **Upside** cases. **+ New version** saves a revised model.
- **Acquisitions**: an analysis of buying a business. **+ Analyze a business** starts one.
- **Documents**: drafts such as a business plan, written from your records. **Read draft** opens one; **+ Generate document** makes a new one.
- **Sales to cash**: invoices and payments. **+ Draft invoice**, **Record issue** and **Record payment** record what you did with your invoicing provider and bank. Foundry does not send invoices.
- **History**: every change, who made it and when, and a check that history and current records agree. **Export business records** downloads a copy.
- **Access & AI**: access keys for other people (**+ Create access key**) and the optional AI review, which works only after it is set up (see [AI setup](AI.md)).

The **WORKSPACE** menu at the top of the left side switches between businesses.

## Create your own business

Choose **+ New business** at the top right. (If you started with `--empty`, the page says **Give your idea a foundation.** and offers **Create a business**.) The form asks for:

- **Business name**: what you call it for now.
- **Entry path**: **Start a business** or **Acquire a business**.
- **Concept**: at least ten characters. Describe the buyer, their problem, what you will offer and why it could work.
- **Intended customer**: who you expect to buy.
- **Accountable owner**: the person responsible for the work. It is a name on the records, not a sign-in.
- **Planning jurisdiction**: the country or state whose planning guide to use. Choose **Undecided — review needed** until someone qualified has looked at where you will operate. It picks a planning guide, not a legal entity or tax choice, and it cannot be changed later in this version.
- **Model currency**: a three-letter code such as USD, EUR or GBP. The financial model uses this one currency.
- **This workspace contains synthetic demonstration data.**: leave this unticked for a real business.

**Create workspace** makes the business and its list of steps. Unknowns can stay unknown until you have evidence.

## What the words on screen mean

Records have an **evidence status**:

- **Assumption**: something you believe but have not checked.
- **Reported**: something a person or a document states.
- **Verified**: an owner or reviewer has checked it against evidence that was itself reviewed.

Steps in **Work & decisions** move through states:

- **Pending** or **In progress**: not ready for a decision yet.
- **Prepared**: the work is done and ready for a person to decide on.
- **Approved**: an owner or reviewer approved it, with reviewed evidence, after its prerequisites were met.
- **Submitted**: for steps done outside Foundry, such as a filing: it was sent, and a reviewed receipt is attached.
- **Verified**: a reviewer checked the result.
- **Not applicable**: a reviewer decided, with evidence, that the step does not apply.
- **Needs review**: something it relied on changed, so it reopened.

These states are kept apart on purpose: preparing something is not approving it, and sending something is not the same as confirming it worked.

**Evidence** is the original text you paste in, such as an email, an agreement clause or interview notes, with an optional web address. It cannot be edited after saving; a correction is a new item. It stays **Unverified** until an owner or reviewer records a human review. If a verification is withdrawn, the item is kept and marked **Rejected**, and work that relied on it reopens as **Needs review**. An **external receipt** is evidence that something happened outside Foundry, such as a filing confirmation or a bank receipt.

## Stop, start again, and keep your data safe

- **To stop**, press Ctrl+C in the terminal window. Foundry says it has stopped. Closing the terminal window also stops it.
- **To start again**, open a terminal, `cd` into the same Foundry folder and run `uv run foundry start`. It shows the same key and does not add the example again. Always start from the same folder: Foundry keeps its data in the folder you start it from.
- **Where your data is**: in the `.foundry` folder inside the Foundry folder. `foundry.db` holds your records and `owner.key` holds your sign-in key. Folders whose names start with a dot are hidden; on a Mac, press Command+Shift+. (full stop) in Finder to show them.
- **To back up**, open a second terminal window, `cd` into the Foundry folder and run `uv run foundry backup ~/foundry-backups/foundry-1.db`. Foundry can do this while it is running. It never overwrites a file, so choose a new name each time. The backup holds every business in the workspace and is not encrypted; keep it somewhere private.
- **The sign-in key** is a long secret that lets anyone who has it act as the owner of every business in this workspace. Foundry keeps only a fingerprint of it, so the backup does not contain the key itself. Copy `owner.key` to the same private place as your backups. `uv run foundry token` prints the key again.
- **If `owner.key` is lost** and you have no copy, you cannot sign in as the owner again. Your records stay in `foundry.db`, but there is no command that makes a new owner key for them. Foundry still starts and tells you where it expected the key file.

## Where to go next

- [The walkthrough](WALKTHROUGH.md): from a concept to a first sale, step by step.
- [Security and deployment](../SECURITY.md): what is protected, and what is your job.
- [AI setup](AI.md): connect an AI assistant or the optional AI review.
- [Architecture](ARCHITECTURE.md): how records, evidence and steps fit together.
- [The README](../README.md): the full list of commands.
