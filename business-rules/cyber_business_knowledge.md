## Marketplace Risk Score (MRS)
Evaluates the overall risk of a platform.
Definition: MRS = \frac{0.4 \times \text{vuln\_count} + 0.3 \times \text{event\_count} + 0.3 \times \text{reputation\_score}}{100}
## Transaction Velocity Rate
Measures transaction completion speed.
Definition: TVR = \frac{\text{total\_transactions}}{\text{active\_time\_in\_hours}}
## Compliance Efficiency Index (CEI)
Represents vendor's compliance performance.
Definition: CEI = \frac{\text{compliance\_score}}{\text{violation\_count}}
## Data Protection Efficiency
Efficiency of data protection per vulnerability.
Definition: DPE = \frac{\text{protection\_measures}}{\text{vulnerability\_instances}}
## Anonymity Cost Index
Traceability score per anonymity level.
Definition: ACI = \frac{\text{traceability\_cost}}{\text{anonymity\_score}}
## Buyer Risk Dollar Ratio
Risk per dollar spent by a buyer.
Definition: BRDR = \frac{\text{risk\_score}}{\text{total\_spending\_usd}}
## Platform Liquidity Rate
Liquidity as a function of transaction flow and price.
Definition: PLR = \frac{\text{successful\_txns} \times \text{average\_price}}{\text{days\_active}}
## Threat Handling Rate
How efficiently threats are managed over time.
Definition: THR = \frac{\text{threats\_handled}}{\text{total\_hours}}
## Suspicion Signal Density
Keyword hits per message volume in communication.
Definition: SSD = \frac{\text{keyword\_matches}}{\text{total\_messages}}
## Wallet Turnover Rate
Turnover of funds in a crypto wallet.
Definition: WTR = \frac{\text{value\_moved}}{\text{wallet\_age\_in\_days}}
## High Risk Vendor
A vendor flagged with active investigation or high law-enforcement interest.
Definition: A vendor is deemed High Risk if there is an active regulatory or law-enforcement investigation in progress, or formal records indicate high interest from authorities.
## Cross-Border Transaction
Indicates the shipment moves across national boundaries.
Definition: Any transaction where the shipment passes from one country to another, triggering customs or import controls.
## Advanced Verification Tier
Vendors with strong identity validation.
Definition: Vendors who have completed multi factor or document backed identity validation are classified as Advanced tier.
## Escrow Compliance
Measures vendor behavior towards using escrow.
Definition: A vendor is Escrow Compliant when they consistently route payments through an escrow mechanism until delivery is confirmed.
## Premium Authentication
Sessions with multi-factor authentication or 2FA.
Definition: Sessions secured by two factor or multi factor authentication are considered to have Premium authentication.
## Traceable Communication
Communications with high traceability signals.
Definition: Message threads exhibiting a high volume of flagged keywords and linguistic anomalies are marked as Traceable for further review.
## Suspicious Buyer
Buyers with low behavior consistency or high risk ratio.
Definition: A buyer is labeled Suspicious when their behavioural consistency score is low and their risk per dollar metric is notably high.
## Secure Platform
Platforms with low vulnerabilities and high protection count.
Definition: A platform with fewer than ten unresolved security vulnerabilities and more than fifteen active protection measures qualifies as Secure.
## Fraud-Flagged Transaction
Transaction flagged by fraud model probability.
Definition: Transactions with a machine learning fraud probability exceeding 70 percent are designated as Fraud Flagged.
## Tier-3 Escalation Case
Alert cases escalated to the highest priority tier.
Definition: Alert cases escalated to Tier-3 represent the highest priority and warrant immediate incident response action.
## Platform Operational Status
Indicates current state of platform functionality.
Definition: Values: Active, Closed, Suspended, Under Investigation. Label descriptions: - Active: The marketplace is online, accepting new listings and processing transactions normally.- Closed: Operators have permanently shut down the marketplace; no further log ins or transactions are possible. - Suspended: The marketplace is temporarily offline, often due to policy violations or maintenance, and may return to service. - Under Investigation: Law enforcement or compliance review is in progress; user activity is restricted or frozen.
## Vendor Access Levels
Privileges granted to vendor accounts.
Definition: Values: Full, Partial. Label descriptions: - Full: Vendor can create listings, modify inventory, withdraw funds, and communicate without restriction. - Partial: Vendor functionality is limited, typically view only or blocked from withdrawals until verification is complete.
## Buyer Authentication Levels
Levels of login verification for buyers.
Definition: Values: Advanced, Basic. Label descriptions: - Advanced: Account protected by multi factor authentication or hardware-backed credentials, offering high assurance. - Basic: Account relies on single factor authentication such as a password, with no secondary verification.
## Product Categories
The general type of products listed.
Definition: Values: Data, Digital, Physical, Service. Label descriptions: - Data: Digital information assets such as credential dumps, personal records, or proprietary databases. - Digital: Intangible goods like software licenses, media subscriptions, or downloadable files. - Physical: Tangible merchandise shipped to the buyer, including hardware devices or printed documents. - Service: Intangible labor or expertise, e.g., penetration testing, content creation, or laundering assistance.
## Shipping Route Complexity
Describes the complexity of delivery routes.
Definition: Values: Simple, Medium, Complex.
## Session Anonymity Levels
How anonymous a session appears.
Definition: Values: High, Medium, Low.
## Alert Severity Levels
Importance level of alerts.
Definition: Values: Critical, High, Medium, Low.
## Escrow Usage States
Whether escrow was used in a transaction.
Definition: Values: Yes, No.
Label descriptions:
- Yes: Funds were held in escrow pending delivery confirmation or dispute resolution, reducing counterparty risk.
- No: Payment was released directly without escrow protection, increasing the chance of fraud.
## Language Patterns
Communication grammar detected in messages.
Definition: Values: Consistent, Suspicious, Variable.
Label descriptions:
- Consistent: Uniform grammar, vocabulary, and tone across messages, suggesting a single genuine author.
- Suspicious: Irregular phrasing, sudden language switches, or machine translated text indicating possible deception.
- Variable: Mixed linguistic styles from multiple senders or intentionally altered patterns to hinder profiling.
## Spend Pattern Categories
Buyer spending trends.
Definition: Values: High, Low, Medium, Variable.
Label descriptions:
- High: Frequent, high value purchases indicative of heavy marketplace engagement.
- Low: Infrequent, low value purchases typical of casual or opportunistic buyers.
- Medium: Steady purchasing cadence with moderate transaction amounts.
- Variable: Irregular bursts of spending with no predictable pattern.
Cached execution: 0
Query internal execution time: 2.934855 milliseconds
