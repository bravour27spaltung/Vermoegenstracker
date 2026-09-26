"""Datenmodell des Vermögenstrackers (SQLAlchemy 2.0).

Konventionen
- Geldbeträge: ganze Cent als BigInteger, Spaltenname endet auf *_cent.
- Verbindlichkeiten werden als positiver Betrag gespeichert und über Position.kind
  abgezogen. Dadurch bleiben Werte in der Eingabe intuitiv (Restschuld 150.000 statt -150.000).
- Quoten und Zinssätze (keine Geldbeträge) sind Float, z. B. 0.035 für 3,5 %.
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PositionKind(str, enum.Enum):
    ASSET = "ASSET"          # Vermögenswert
    LIABILITY = "LIABILITY"  # Verbindlichkeit


class AssetClass(str, enum.Enum):
    LIQUIDITY = "LIQUIDITY"      # Giro, Tagesgeld, Festgeld
    EQUITIES = "EQUITIES"        # Aktien, Aktienfonds, ETFs
    BONDS = "BONDS"              # Anleihen, Rentenfonds
    REAL_ESTATE = "REAL_ESTATE"  # Immobilien
    CRYPTO = "CRYPTO"
    COMMODITIES = "COMMODITIES"  # Gold, Rohstoffe
    PENSION = "PENSION"          # Riester, Rürup, private Rentenversicherung (GRV/bAV: PensionContract)
    OTHER = "OTHER"


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Portabler Enum-Typ: als VARCHAR plus CHECK-Constraint (statt nativem Enum)."""
    return Enum(enum_cls, native_enum=False, length=20, create_constraint=True, name=name)


class Position(Base):
    """Eine Vermögens- oder Schuldenposition, z. B. 'Welt-ETF-Depot' oder 'Baudarlehen'."""

    __tablename__ = "position"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'ASSET' AND asset_class IS NOT NULL) "
            "OR (kind = 'LIABILITY' AND asset_class IS NULL)",
            name="ck_position_class_matches_kind",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[PositionKind] = mapped_column(_enum(PositionKind, "position_kind"))
    # Nur für Vermögenswerte; bei Verbindlichkeiten NULL.
    asset_class: Mapped[Optional[AssetClass]] = mapped_column(
        _enum(AssetClass, "position_asset_class"), default=None
    )
    # Kurzfristig verfügbar (zählt für die Liquiditätsquote). Festgeld kann Klasse
    # LIQUIDITY haben und trotzdem is_liquid=False sein.
    is_liquid: Mapped[bool] = mapped_column(Boolean, default=False)
    # Erzeugt im Ruhestand Entnahmen (Depot: ja, selbst genutzte Immobilie: nein).
    retirement_relevant: Mapped[bool] = mapped_column(Boolean, default=True)
    # Einzelwert (z. B. eine Aktie) statt breit gestreutem Fonds. Relevant für Klumpenrisiko.
    is_single_security: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), default=None)

    snapshots: Mapped[list[Snapshot]] = relationship(back_populates="position")


class Snapshot(Base):
    """Wert einer Position an einem Stichtag (Quartals- oder Jahresende)."""

    __tablename__ = "snapshot"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "position_id", name="uq_snapshot_date_position"),
        CheckConstraint("value_cent >= 0", name="ck_snapshot_value_nonnegative"),
        Index("ix_snapshot_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id", ondelete="RESTRICT"))
    value_cent: Mapped[int] = mapped_column(BigInteger)
    # Saldo der Ein- und Auszahlungen seit dem vorherigen Stichtag
    # (Einzahlung positiv, Entnahme negativ). Grundlage für Sparrate und Rendite
    # (Modified Dietz / XIRR). Bei Verbindlichkeiten ohne Bedeutung, dort 0 lassen.
    net_contribution_cent: Mapped[int] = mapped_column(BigInteger, default=0)

    position: Mapped[Position] = relationship(back_populates="snapshots")


class ExpenseRecord(Base):
    """Monatliche Nettoausgaben je Stichtag (Nenner der Liquiditätsquote)."""

    __tablename__ = "expense_record"
    __table_args__ = (CheckConstraint("monthly_expenses_cent >= 0", name="ck_expense_nonnegative"),)

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    monthly_expenses_cent: Mapped[int] = mapped_column(BigInteger)


class ExpenseCadence(str, enum.Enum):
    """Zahlungsturnus einer Ausgabenkategorie. Bestimmt, worauf sich der erfasste
    Betrag bezieht (z. B. eine KFZ-Versicherung, die einmal im Jahr fällig wird) –
    nicht, wie oft du ihn im Stichtagsformular überprüfst oder aktualisierst."""

    MONTHLY = "MONTHLY"      # z. B. Miete, Handyvertrag
    QUARTERLY = "QUARTERLY"  # z. B. manche Versicherungsbeiträge
    YEARLY = "YEARLY"        # z. B. KFZ-Versicherung, Rundfunkbeitrag im Voraus


# Anzahl Monate je Turnus – Grundlage, um Beträge auf einen Monatswert umzurechnen
# und so über Kategorien mit unterschiedlichem Turnus hinweg vergleichbar zu machen.
CADENCE_MONTHS: dict[ExpenseCadence, int] = {
    ExpenseCadence.MONTHLY: 1,
    ExpenseCadence.QUARTERLY: 3,
    ExpenseCadence.YEARLY: 12,
}


class ExpenseCategory(Base):
    """Eine wiederkehrende Ausgabenkategorie, z. B. 'Miete' oder 'KFZ-Versicherung'.

    Getrennt von ExpenseRecord (der eine einzelne Gesamtsumme pro Jahr für die
    Liquiditätsquote im Jahresupdate liefert): hier geht es um die Entwicklung
    einzelner Kategorien über die Zeit. Gleiches Muster wie Position/Snapshot.
    """

    __tablename__ = "expense_category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    cadence: Mapped[ExpenseCadence] = mapped_column(
        _enum(ExpenseCadence, "expense_cadence"), default=ExpenseCadence.MONTHLY
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), default=None)

    records: Mapped[list["ExpenseCategoryRecord"]] = relationship(
        back_populates="category", order_by="ExpenseCategoryRecord.snapshot_date"
    )


class ExpenseCategoryRecord(Base):
    """Betrag einer Ausgabenkategorie zu einem Stichtag – bezogen auf EINEN
    Zahlungsturnus der Kategorie (siehe ExpenseCategory.cadence), nicht zwingend
    auf einen Monat. Bei einer jährlichen KFZ-Versicherung steht hier also die
    Jahresprämie, nicht ein Zwölftel davon."""

    __tablename__ = "expense_category_record"
    __table_args__ = (
        UniqueConstraint("category_id", "snapshot_date", name="uq_expense_category_record_date"),
        CheckConstraint("amount_cent >= 0", name="ck_expense_category_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_category.id", ondelete="RESTRICT"))
    snapshot_date: Mapped[date] = mapped_column(Date)
    amount_cent: Mapped[int] = mapped_column(BigInteger)

    category: Mapped[ExpenseCategory] = relationship(back_populates="records")


class Profile(Base):
    """Persönliche Ziele und Annahmen. Genau eine Zeile (id = 1).

    Alle Standardwerte sind Platzhalter und müssen vom Nutzer gesetzt werden.
    Beträge in heutiger Kaufkraft (real), Renditeannahme ebenfalls real.
    """

    __tablename__ = "profile"
    __table_args__ = (CheckConstraint("id = 1", name="ck_profile_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    birth_date: Mapped[Optional[date]] = mapped_column(Date, default=None)
    retirement_age: Mapped[int] = mapped_column(Integer, default=67)
    desired_income_monthly_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    # Laut jährlicher Renteninformation (brutto, nominal): bewusst als eigene Annahme führen.
    statutory_pension_monthly_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    planned_savings_monthly_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    withdrawal_rate: Mapped[float] = mapped_column(Float, default=0.035)
    expected_real_return: Mapped[float] = mapped_column(Float, default=0.03)
    inflation_assumption: Mapped[float] = mapped_column(Float, default=0.02)
    emergency_months_target: Mapped[float] = mapped_column(Float, default=6.0)
    concentration_limit: Mapped[float] = mapped_column(Float, default=0.25)


class TargetAllocation(Base):
    """Soll-Anteil je Anlageklasse mit Toleranzband (Anteile als 0..1)."""

    __tablename__ = "target_allocation"
    __table_args__ = (
        CheckConstraint("target_share >= 0 AND target_share <= 1", name="ck_target_share_range"),
        CheckConstraint("band >= 0 AND band <= 1", name="ck_target_band_range"),
    )

    asset_class: Mapped[AssetClass] = mapped_column(
        _enum(AssetClass, "target_asset_class"), primary_key=True
    )
    target_share: Mapped[float] = mapped_column(Float)
    band: Mapped[float] = mapped_column(Float, default=0.05)


class CpiIndex(Base):
    """Verbraucherpreisindex (z. B. Destatis), ein Wert je Monat (Datum = 1. des Monats)."""

    __tablename__ = "cpi_index"
    __table_args__ = (CheckConstraint("index_value > 0", name="ck_cpi_positive"),)

    period: Mapped[date] = mapped_column(Date, primary_key=True)
    index_value: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------------------
# Rentenansprüche (gesetzliche Rente, Betriebsrente)
#
# Bewusst getrennt von Position/Snapshot: Anwartschaften sind nicht verkäuflich,
# nicht beleihbar, nicht vererbbar und nicht liquide. Sie fließen deshalb nicht ins
# Nettovermögen ein, sondern werden als monatliche Rente (netto, heutige Kaufkraft)
# und optional als Barwert ausgewiesen (siehe app/pension.py).
# ---------------------------------------------------------------------------


class PensionType(str, enum.Enum):
    STATUTORY = "STATUTORY"          # gesetzliche Rentenversicherung (DRV)
    OCCUPATIONAL = "OCCUPATIONAL"    # betriebliche Altersversorgung (bAV)


class ValueBasis(str, enum.Enum):
    NOMINAL = "NOMINAL"  # Euro des Auszahlungsjahres (typisch für bAV-Standmitteilungen)
    REAL = "REAL"        # bereits in heutiger Kaufkraft


class PensionContract(Base):
    """Ein Rentenanspruch, z. B. 'DRV' oder 'Direktversicherung Arbeitgeber X'."""

    __tablename__ = "pension_contract"
    __table_args__ = (
        CheckConstraint("net_ratio > 0 AND net_ratio <= 1", name="ck_pension_net_ratio_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    pension_type: Mapped[PensionType] = mapped_column(_enum(PensionType, "pension_type"))
    # Durchführungsweg bei bAV: Direktversicherung, Pensionskasse, Pensionsfonds,
    # Direktzusage, Unterstützungskasse. Bei GRV leer.
    vehicle: Mapped[Optional[str]] = mapped_column(String(60), default=None)
    # Standard: Steuer und Kranken-/Pflegeversicherung werden berechnet (app/pension.py).
    # Nur wenn manual_net gesetzt ist, gilt stattdessen die pauschale Netto-Quote net_ratio,
    # z. B. für Sonderfälle wie eine bAV aus dem Ausland.
    manual_net: Mapped[bool] = mapped_column(Boolean, default=False)
    net_ratio: Mapped[float] = mapped_column(Float, default=0.75)
    # Unverfallbar nach § 1b BetrAVG. Verfallbare Ansprüche werden angezeigt, aber markiert.
    is_vested: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), default=None)

    snapshots: Mapped[list[PensionSnapshot]] = relationship(
        back_populates="contract", order_by="PensionSnapshot.as_of"
    )


class PensionSnapshot(Base):
    """Stand eines Rentenanspruchs laut Renteninformation bzw. Standmitteilung.

    Alle Felder außer as_of sind optional, weil die Unterlagen je nach Träger andere
    Werte ausweisen. Die Rechenlogik nimmt jeweils den besten verfügbaren Wert.

    GRV: earning_points, pension_value_cent (Rentenwert zum Stichtag),
         accrued_monthly_cent (bisher erreicht), projected_monthly_cent
         (Hochrechnung OHNE Rentenanpassung). value_basis wird ignoriert.
    bAV: guaranteed_*/projected_* als Monatsrente und/oder Kapital,
         current_value_cent (Deckungskapital/Vertragsguthaben), value_basis.
    """

    __tablename__ = "pension_snapshot"
    __table_args__ = (
        UniqueConstraint("contract_id", "as_of", name="uq_pension_snapshot_contract_date"),
        CheckConstraint(
            "earning_points IS NULL OR earning_points >= 0", name="ck_pension_ep_nonnegative"
        ),
        CheckConstraint(
            "pension_value_cent IS NULL OR pension_value_cent > 0",
            name="ck_pension_value_positive",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("pension_contract.id", ondelete="RESTRICT")
    )
    as_of: Mapped[date] = mapped_column(Date)
    value_basis: Mapped[ValueBasis] = mapped_column(
        _enum(ValueBasis, "pension_value_basis"), default=ValueBasis.NOMINAL
    )

    # Gesetzliche Rente
    earning_points: Mapped[Optional[float]] = mapped_column(Float, default=None)
    pension_value_cent: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    accrued_monthly_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)

    # Beide Arten
    projected_monthly_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)

    # Betriebsrente
    guaranteed_monthly_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    guaranteed_capital_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    projected_capital_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    current_value_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    employee_contribution_pa_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    employer_contribution_pa_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)

    contract: Mapped[PensionContract] = relationship(back_populates="snapshots")


class HealthInsurance(str, enum.Enum):
    GKV = "GKV"  # gesetzlich (Krankenversicherung der Rentner / freiwillig)
    PKV = "PKV"  # privat


class PkvEstimateMode(str, enum.Enum):
    PROJECTED = "PROJECTED"  # aus heutigem Beitrag hochrechnen (Standard)
    DIRECT = "DIRECT"        # Beitrag im Alter direkt schätzen


class PensionSettings(Base):
    """Rentenspezifische Annahmen. Genau eine Zeile (id = 1).

    Eigene Tabelle statt neuer Spalten in Profile, damit bestehende Datenbanken ohne
    Migration weiterlaufen (create_all legt nur fehlende Tabellen an).
    """

    __tablename__ = "pension_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_pension_settings_single_row"),
        CheckConstraint("payout_years > 0", name="ck_pension_payout_years_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Aktueller Rentenwert in Cent. 4252 = 42,52 € (gültig ab 1.7.2026).
    current_pension_value_cent: Mapped[int] = mapped_column(Integer, default=4252)
    # Angenommene Bezugsdauer in Jahren (bewusst eher lang: Langlebigkeitsrisiko).
    payout_years: Mapped[int] = mapped_column(Integer, default=25)
    # Reale Rendite in der Entnahmephase (eher defensiver als in der Ansparphase).
    payout_real_return: Mapped[float] = mapped_column(Float, default=0.01)

    # Krankenversicherung im Ruhestand. Bestimmt, wie Renten netto gerechnet werden.
    health_insurance: Mapped[HealthInsurance] = mapped_column(
        _enum(HealthInsurance, "health_insurance"), default=HealthInsurance.PKV
    )
    pkv_mode: Mapped[PkvEstimateMode] = mapped_column(
        _enum(PkvEstimateMode, "pkv_estimate_mode"), default=PkvEstimateMode.PROJECTED
    )
    # Modus PROJECTED: heutige Monatsbeiträge laut Beitragsrechnung (Gesamtbeitrag, also vor
    # Arbeitgeberzuschuss) und die Beitragsentlastung laut Vertrag. Siehe pension.project_pkv.
    pkv_today_total_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    pkv_today_sick_pay_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    pkv_today_surcharge_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    pkv_today_relief_contribution_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    # Monatliche Beitragsentlastung ab Rentenbeginn, nominal (fester Euro-Betrag laut Vertrag).
    pkv_relief_benefit_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    pkv_today_care_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    # Reale Beitragssteigerung p. a. (über der Inflation). WIP: nominal 3,1-3,9 % p. a.
    pkv_real_increase: Mapped[float] = mapped_column(Float, default=0.015)
    # Modus DIRECT: erwartete Monatsbeiträge im Ruhestand in heutiger Kaufkraft.
    pkv_health_monthly_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    pkv_care_monthly_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    # Anteil des PKV-Beitrags für die Basisabsicherung (steuerlich voll absetzbar, § 10 Abs. 1
    # Nr. 3 EStG). Steht in der Beitragsbescheinigung des Versicherers; typisch 75-90 %.
    pkv_basic_share: Mapped[float] = mapped_column(Float, default=0.8)


# ---------------------------------------------------------------------------
# Depots (Wertpapierebene)
#
# Ergänzt Position/Snapshot um die Sicht auf einzelne Wertpapiere: Käufe, Verkäufe,
# Ausschüttungen und Kurse je Stichtag. Daraus rechnet app/depot.py Einstand (FIFO),
# Rendite (XIRR), Steuer und Prognose. Über DepotPositionLink lassen sich Wert und
# Netto-Einzahlungen je Anlageklasse als Snapshot in die Positionen übernehmen, damit
# Nettovermögen und Rentenlücke die Depotdaten nutzen.
# ---------------------------------------------------------------------------


class Depot(Base):
    """Ein Wertpapierdepot bei einem Broker, z. B. 'Trade Republic'."""

    __tablename__ = "depot"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    broker: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    # Summe aller Sparpläne in diesem Depot (Basis der Prognose).
    monthly_savings_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), default=None)

    transactions: Mapped[list[DepotTransaction]] = relationship(
        back_populates="depot", order_by="DepotTransaction.trade_date"
    )


class Security(Base):
    """Stammdaten eines Wertpapiers. Anlageklasse wie bei Position (ohne PENSION)."""

    __tablename__ = "security"
    __table_args__ = (
        CheckConstraint("ter >= 0 AND ter <= 0.1", name="ck_security_ter_range"),
        CheckConstraint(
            "partial_exemption >= 0 AND partial_exemption <= 1", name="ck_security_exemption_range"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ISIN; bei Krypto das Kürzel des Brokers (z. B. 'BTC').
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    asset_class: Mapped[AssetClass] = mapped_column(
        _enum(AssetClass, "security_asset_class"), default=AssetClass.EQUITIES
    )
    region: Mapped[str] = mapped_column(String(60), default="Welt")
    # Laufende Kosten p. a. als Quote (0.002 = 0,20 %).
    ter: Mapped[float] = mapped_column(Float, default=0.0)
    # Investmentfonds i. S. d. InvStG (ETF/Fonds): Vorabpauschale und Teilfreistellung.
    is_fund: Mapped[bool] = mapped_column(Boolean, default=True)
    # Teilfreistellung § 20 InvStG: 0.3 Aktienfonds, 0.15 Mischfonds, 0.6/0.8 Immobilienfonds.
    partial_exemption: Mapped[float] = mapped_column(Float, default=0.3)


class TransactionKind(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"  # Ausschüttung/Dividende, netto nach Steuern


class DepotTransaction(Base):
    """Kauf, Verkauf oder Ausschüttung.

    amount_cent ist der Kurswert (Stück × Kurs) ohne Gebühren, bei Ausschüttungen der
    Bruttobetrag vor Steuern. Stückzahlen sind Float (Bruchstücke bei Sparplänen), Geld bleibt in Cent.
    """

    __tablename__ = "depot_transaction"
    __table_args__ = (
        CheckConstraint("amount_cent >= 0", name="ck_depot_tx_amount_nonnegative"),
        CheckConstraint("fee_cent >= 0", name="ck_depot_tx_fee_nonnegative"),
        CheckConstraint("quantity >= 0", name="ck_depot_tx_quantity_nonnegative"),
        Index("ix_depot_tx_depot_date", "depot_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    depot_id: Mapped[int] = mapped_column(ForeignKey("depot.id", ondelete="RESTRICT"))
    security_id: Mapped[int] = mapped_column(ForeignKey("security.id", ondelete="RESTRICT"))
    trade_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[TransactionKind] = mapped_column(_enum(TransactionKind, "depot_tx_kind"))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    amount_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    # Einbehaltene Steuern (Abgeltungsteuer/Soli/KiSt, Quellensteuer), positiv. Nur zur Info:
    # Rendite und Einstand werden vor Steuern gerechnet.
    tax_cent: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # Buchungs-ID des Brokers (z. B. transaction_id bei Trade Republic). Verhindert Doppelimporte.
    external_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)

    depot: Mapped[Depot] = relationship(back_populates="transactions")
    security: Mapped[Security] = relationship()


class SecurityPrice(Base):
    """Kurs eines Wertpapiers an einem Stichtag (manuell gepflegt, z. B. quartalsweise)."""

    __tablename__ = "security_price"
    __table_args__ = (CheckConstraint("price_cent >= 0", name="ck_security_price_nonnegative"),)

    security_id: Mapped[int] = mapped_column(
        ForeignKey("security.id", ondelete="CASCADE"), primary_key=True
    )
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    price_cent: Mapped[int] = mapped_column(BigInteger)


class DepotPositionLink(Base):
    """Welche Position den Anteil einer Anlageklasse eines Depots aufnimmt.

    Mehrere Depots dürfen auf dieselbe Position zeigen; Werte werden dann summiert.
    """

    __tablename__ = "depot_position_link"

    depot_id: Mapped[int] = mapped_column(ForeignKey("depot.id", ondelete="CASCADE"), primary_key=True)
    asset_class: Mapped[AssetClass] = mapped_column(
        _enum(AssetClass, "depot_link_asset_class"), primary_key=True
    )
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id", ondelete="RESTRICT"))


class ReturnAssumption(Base):
    """Reale Rendite (geometrisch, p. a.) und Volatilität je Anlageklasse für die Depotprognose."""

    __tablename__ = "return_assumption"
    __table_args__ = (CheckConstraint("volatility >= 0", name="ck_return_vol_nonnegative"),)

    asset_class: Mapped[AssetClass] = mapped_column(
        _enum(AssetClass, "return_asset_class"), primary_key=True
    )
    real_return: Mapped[float] = mapped_column(Float)
    volatility: Mapped[float] = mapped_column(Float)


class DepotSettings(Base):
    """Steuer- und Prognoseparameter der Depots. Genau eine Zeile (id = 1).

    Inflation und Klumpenschwelle kommen aus Profile (inflation_assumption, concentration_limit).
    """

    __tablename__ = "depot_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_depot_settings_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Kirchensteuersatz als Quote: 0, 0.08 (BW, BY) oder 0.09.
    church_tax_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # Sparerpauschbetrag § 20 Abs. 9 EStG: 1.000 € (Einzel), 2.000 € (Zusammenveranlagung).
    saver_allowance_cent: Mapped[int] = mapped_column(BigInteger, default=100000)
    # Basiszins für die Vorabpauschale (§ 18 Abs. 4 InvStG). 0.032 = Basiszins zum 2.1.2026.
    base_rate: Mapped[float] = mapped_column(Float, default=0.032)
    base_rate_year: Mapped[int] = mapped_column(Integer, default=2026)
    mc_paths: Mapped[int] = mapped_column(Integer, default=2000)
    ter_warn: Mapped[float] = mapped_column(Float, default=0.005)
    # Ziel-Aktienquote innerhalb der Depots (0..1) mit Toleranzband. NULL = keine Prüfung.
    equity_target: Mapped[Optional[float]] = mapped_column(Float, default=None)
    equity_band: Mapped[float] = mapped_column(Float, default=0.05)


class FireSettings(Base):
    """Annahmen für die FIRE-Rechnung. Genau eine Zeile (id = 1).

    Leere Felder (NULL) bedeuten „aus Profil/Rente ableiten“: Ausgaben = Wunscheinkommen,
    Krankenversicherung = heutiger PKV-Beitrag ohne Krankentagegeld plus Pflege.
    Entnahmerate und Inflation stehen im Profil.
    """

    __tablename__ = "fire_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_fire_settings_single_row"),
        CheckConstraint("target_success > 0 AND target_success < 1", name="ck_fire_target_success_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Netto-Ausgaben pro Monat nach FIRE, ohne Krankenversicherung (real).
    spending_monthly_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    # Kranken- und Pflegeversicherung pro Monat nach FIRE bis Rentenbeginn (real, voller Beitrag).
    health_monthly_cent: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    # Vermögen soll bis zu diesem Alter reichen (Langlebigkeitsrisiko).
    horizon_age: Mapped[int] = mapped_column(Integer, default=95)
    # Geforderte Erfolgswahrscheinlichkeit der Simulation.
    target_success: Mapped[float] = mapped_column(Float, default=0.9)
    # Beginn der Beitragszeiten, falls die bisher erreichte Rente nicht erfasst ist.
    career_start_age: Mapped[int] = mapped_column(Integer, default=22)
