"""Gera os CIDs de exemplo (sintéticos) usados nos testes e na demonstração.

ATENÇÃO: não são os CIDs reais da planta. O exemplo do i15 imita a estrutura descrita nas
regras de negócio (IED SE_BAB__H06_BCM1_MD2, sAutGGIO2.Ind3 = MOLA, RCBs só no LLN0).
"""
from pathlib import Path

TEMPLATES = """
  <DataTypeTemplates>
    <LNodeType id="LLN0_T" lnClass="LLN0">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Health" type="INS_Health"/><DO name="NamPlt" type="LPL_LN0"/>
    </LNodeType>
    <LNodeType id="LPHD_T" lnClass="LPHD">
      <DO name="PhyNam" type="DPL_T"/><DO name="PhyHealth" type="INS_Health"/><DO name="Proxy" type="SPS_T"/>
    </LNodeType>
    <LNodeType id="GGIO_T" lnClass="GGIO">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Health" type="INS_Health"/><DO name="NamPlt" type="LPL_T"/>
      <DO name="Ind1" type="SPS_T"/><DO name="Ind2" type="SPS_T"/><DO name="Ind3" type="SPS_T"/><DO name="Ind4" type="SPS_T"/>
      <DO name="Ind5" type="SPS_T"/><DO name="Ind6" type="SPS_T"/><DO name="Ind7" type="SPS_T"/><DO name="Ind8" type="SPS_T"/>
      <DO name="IntIn1" type="INS_T"/>
    </LNodeType>
    <LNodeType id="XCBR_T" lnClass="XCBR">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Health" type="INS_Health"/><DO name="NamPlt" type="LPL_T"/>
      <DO name="Loc" type="SPS_T"/><DO name="OpCnt" type="INS_T"/><DO name="Pos" type="DPC_T"/>
    </LNodeType>
    <LNodeType id="PTOC_T" lnClass="PTOC">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Str" type="ACD_T"/><DO name="Op" type="ACT_T"/>
    </LNodeType>
    <LNodeType id="MMXU_T" lnClass="MMXU">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="TotW" type="MV_T"/><DO name="TotVAr" type="MV_T"/>
      <DO name="Hz" type="MV_Hz"/><DO name="PhV" type="WYE_T"/><DO name="A" type="WYE_T"/>
    </LNodeType>
    <LNodeType id="MMTR_T" lnClass="MMTR">
      <DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="TotWh" type="BCR_T"/>
    </LNodeType>
    <DOType id="INC_Mod" cdc="INC">
      <DA name="stVal" fc="ST" bType="Enum" type="Mod" dchg="true"><Val>on</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
      <DA name="ctlModel" fc="CF" bType="Enum" type="ctlModel"><Val>status-only</Val></DA>
    </DOType>
    <DOType id="INS_Beh" cdc="INS">
      <DA name="stVal" fc="ST" bType="Enum" type="Beh" dchg="true"><Val>on</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
    </DOType>
    <DOType id="INS_Health" cdc="INS">
      <DA name="stVal" fc="ST" bType="Enum" type="Health" dchg="true"><Val>Ok</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
    </DOType>
    <DOType id="INS_T" cdc="INS">
      <DA name="stVal" fc="ST" bType="INT32" dchg="true"/><DA name="q" fc="ST" bType="Quality" qchg="true"/>
      <DA name="t" fc="ST" bType="Timestamp"/>
    </DOType>
    <DOType id="LPL_LN0" cdc="LPL">
      <DA name="vendor" fc="DC" bType="VisString255"/><DA name="swRev" fc="DC" bType="VisString255"/>
      <DA name="d" fc="DC" bType="VisString255"/><DA name="configRev" fc="DC" bType="VisString255"/>
      <DA name="ldNs" fc="EX" bType="VisString255"><Val>IEC 61850-7-4:2003</Val></DA>
    </DOType>
    <DOType id="LPL_T" cdc="LPL">
      <DA name="vendor" fc="DC" bType="VisString255"/><DA name="swRev" fc="DC" bType="VisString255"/>
      <DA name="d" fc="DC" bType="VisString255"/>
    </DOType>
    <DOType id="DPL_T" cdc="DPL">
      <DA name="vendor" fc="DC" bType="VisString255"/><DA name="hwRev" fc="DC" bType="VisString255"/>
      <DA name="serNum" fc="DC" bType="VisString255"/><DA name="model" fc="DC" bType="VisString255"/>
    </DOType>
    <DOType id="SPS_T" cdc="SPS">
      <DA name="stVal" fc="ST" bType="BOOLEAN" dchg="true"/><DA name="q" fc="ST" bType="Quality" qchg="true"/>
      <DA name="t" fc="ST" bType="Timestamp"/><DA name="d" fc="DC" bType="VisString255"/>
    </DOType>
    <DOType id="DPC_T" cdc="DPC">
      <DA name="stVal" fc="ST" bType="Dbpos" dchg="true"><Val>off</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
      <DA name="ctlModel" fc="CF" bType="Enum" type="ctlModel"><Val>status-only</Val></DA>
    </DOType>
    <DOType id="ACT_T" cdc="ACT">
      <DA name="general" fc="ST" bType="BOOLEAN" dchg="true"/><DA name="q" fc="ST" bType="Quality" qchg="true"/>
      <DA name="t" fc="ST" bType="Timestamp"/>
    </DOType>
    <DOType id="ACD_T" cdc="ACD">
      <DA name="general" fc="ST" bType="BOOLEAN" dchg="true"/><DA name="dirGeneral" fc="ST" bType="Enum" type="dir" dchg="true"/>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
    </DOType>
    <DOType id="MV_T" cdc="MV">
      <DA name="mag" fc="MX" bType="Struct" type="AnalogueValue_T" dchg="true"/>
      <DA name="q" fc="MX" bType="Quality" qchg="true"/><DA name="t" fc="MX" bType="Timestamp"/>
      <DA name="units" fc="CF" bType="Struct" type="Unit_W"/><DA name="db" fc="CF" bType="INT32U"><Val>0</Val></DA>
    </DOType>
    <DOType id="MV_Hz" cdc="MV">
      <DA name="mag" fc="MX" bType="Struct" type="AnalogueValue_T" dchg="true"/>
      <DA name="q" fc="MX" bType="Quality" qchg="true"/><DA name="t" fc="MX" bType="Timestamp"/>
      <DA name="units" fc="CF" bType="Struct" type="Unit_Hz"/><DA name="db" fc="CF" bType="INT32U"><Val>100</Val></DA>
      <DA name="zeroDb" fc="CF" bType="INT32U"><Val>0</Val></DA>
      <DA name="rangeC" fc="CF" bType="Struct" type="RangeConfig_Hz"/>
    </DOType>
    <DOType id="CMV_T" cdc="CMV">
      <DA name="cVal" fc="MX" bType="Struct" type="Vector_T" dchg="true"/>
      <DA name="q" fc="MX" bType="Quality" qchg="true"/><DA name="t" fc="MX" bType="Timestamp"/>
      <DA name="db" fc="CF" bType="INT32U"><Val>0</Val></DA>
    </DOType>
    <DOType id="WYE_T" cdc="WYE">
      <SDO name="phsA" type="CMV_T"/><SDO name="phsB" type="CMV_T"/><SDO name="phsC" type="CMV_T"/>
    </DOType>
    <DOType id="BCR_T" cdc="BCR">
      <DA name="actVal" fc="ST" bType="INT64" dchg="true"/><DA name="q" fc="ST" bType="Quality" qchg="true"/>
      <DA name="t" fc="ST" bType="Timestamp"/><DA name="pulsQty" fc="CF" bType="FLOAT32"><Val>1</Val></DA>
    </DOType>
    <DAType id="AnalogueValue_T"><BDA name="f" bType="FLOAT32"/></DAType>
    <DAType id="Vector_T"><BDA name="mag" bType="Struct" type="AnalogueValue_T"/><BDA name="ang" bType="Struct" type="AnalogueValue_T"/></DAType>
    <DAType id="Unit_W"><BDA name="SIUnit" bType="Enum" type="SIUnit"><Val>W</Val></BDA><BDA name="multiplier" bType="Enum" type="multiplier"><Val>M</Val></BDA></DAType>
    <DAType id="Unit_Hz"><BDA name="SIUnit" bType="Enum" type="SIUnit"><Val>Hz</Val></BDA><BDA name="multiplier" bType="Enum" type="multiplier"><Val></Val></BDA></DAType>
    <DAType id="RangeConfig_Hz">
      <BDA name="hhLim" bType="Struct" type="AnalogueValue_T"/><BDA name="hLim" bType="Struct" type="AnalogueValue_T"/>
      <BDA name="lLim" bType="Struct" type="AnalogueValue_T"/><BDA name="llLim" bType="Struct" type="AnalogueValue_T"/>
      <BDA name="min" bType="Struct" type="AnalogueValue_T"/>
      <BDA name="max" bType="Struct" type="AnalogueValue_T"/>
    </DAType>
    <EnumType id="Mod"><EnumVal ord="1">on</EnumVal><EnumVal ord="2">blocked</EnumVal><EnumVal ord="3">test</EnumVal><EnumVal ord="4">test/blocked</EnumVal><EnumVal ord="5">off</EnumVal></EnumType>
    <EnumType id="Beh"><EnumVal ord="1">on</EnumVal><EnumVal ord="2">blocked</EnumVal><EnumVal ord="3">test</EnumVal><EnumVal ord="4">test/blocked</EnumVal><EnumVal ord="5">off</EnumVal></EnumType>
    <EnumType id="Health"><EnumVal ord="1">Ok</EnumVal><EnumVal ord="2">Warning</EnumVal><EnumVal ord="3">Alarm</EnumVal></EnumType>
    <EnumType id="ctlModel"><EnumVal ord="0">status-only</EnumVal><EnumVal ord="1">direct-with-normal-security</EnumVal><EnumVal ord="2">sbo-with-normal-security</EnumVal><EnumVal ord="3">direct-with-enhanced-security</EnumVal><EnumVal ord="4">sbo-with-enhanced-security</EnumVal></EnumType>
    <EnumType id="dir"><EnumVal ord="0">unknown</EnumVal><EnumVal ord="1">forward</EnumVal><EnumVal ord="2">backward</EnumVal><EnumVal ord="3">both</EnumVal></EnumType>
    <EnumType id="SIUnit"><EnumVal ord="1"></EnumVal><EnumVal ord="5">A</EnumVal><EnumVal ord="9">deg</EnumVal><EnumVal ord="29">V</EnumVal><EnumVal ord="33">Hz</EnumVal><EnumVal ord="38">W</EnumVal><EnumVal ord="63">VAr</EnumVal><EnumVal ord="72">Wh</EnumVal></EnumType>
    <EnumType id="multiplier"><EnumVal ord="-3">m</EnumVal><EnumVal ord="0"></EnumVal><EnumVal ord="3">k</EnumVal><EnumVal ord="6">M</EnumVal></EnumType>
  </DataTypeTemplates>"""

OPT_ALL = '<OptFields seqNum="true" timeStamp="true" dataSet="true" reasonCode="true" dataRef="false" entryID="true" configRef="true" bufOvfl="true"/>'


def i15() -> str:
    ied = "SE_BAB__H06_BCM1_MD2"
    inds = "".join(f'<FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind{i}" fc="ST"/>' for i in range(1, 9))
    descs = {1: "DISJUNTOR - FALHA", 2: "PROTEÇÃO ATUADA", 3: "MOLA", 4: "SF6 - ALARME", 5: "SF6 - BLOQUEIO",
             6: "LOCAL/REMOTO", 7: "FALHA VCC", 8: "RELÉ EM TESTE"}
    dois = "".join(f'<DOI name="Ind{i}" desc="{d}"><DAI name="d"><Val>{d}</Val></DAI></DOI>' for i, d in descs.items())

    def rc(name, ds, buffered, maxn=3, intg=0, period="false", buftime=0):
        return (f'<ReportControl name="{name}" rptID="" datSet="{ds}" confRev="1" buffered="{"true" if buffered else "false"}" '
                f'bufTime="{buftime}" intgPd="{intg}"><TrgOps dchg="true" qchg="true" dupd="false" period="{period}" gi="true"/>'
                f'{OPT_ALL}<RptEnabled max="{maxn}"/></ReportControl>')

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- CID SINTÉTICO de exemplo, imitando a estrutura do Ingeteam INGEPAC EF (i15). Não é o CID real. -->
<SCL xmlns="http://www.iec.ch/61850/2003/SCL" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Header id="SE_BAB_H06_BCM1" version="1" revision="0" toolID="gen_exemplos.py" nameStructure="IEDName"/>
  <Communication>
    <SubNetwork name="SE_BAB_MMS" type="8-MMS">
      <ConnectedAP iedName="{ied}" apName="S1">
        <Address><P type="IP">10.237.68.177</P><P type="IP-SUBNET">255.255.255.0</P><P type="IP-GATEWAY">10.237.68.1</P>
          <P type="OSI-AP-Title">1,3,9999,33</P><P type="OSI-AE-Qualifier">33</P><P type="OSI-PSEL">00000001</P>
          <P type="OSI-SSEL">0001</P><P type="OSI-TSEL">0001</P></Address>
      </ConnectedAP>
    </SubNetwork>
  </Communication>
  <IED name="{ied}" manufacturer="Ingeteam" type="INGEPAC EF" desc="BH06BCM1 - exemplo sintético" configVersion="1.0">
    <Private type="Ingeteam-config">dados privados preservados (RN-CID-07)</Private>
    <Services>
      <DynAssociation/><GetDirectory/><GetDataObjectDefinition/><DataObjectDirectory/><GetDataSetValue/>
      <DataSetDirectory/><ConfDataSet max="16" maxAttributes="100"/><ReadWrite/><ConfReportControl max="32"/>
      <GetCBValues/><ReportSettings cbName="Conf" datSet="Conf" rptID="Dyn" optFields="Dyn" bufTime="Dyn" trgOps="Dyn" intgPd="Dyn"/>
      <ConfLNs fixPrefix="true" fixLnInst="true"/>
    </Services>
    <AccessPoint name="S1">
      <Server>
        <Authentication none="true"/>
        <LDevice inst="CTRL" desc="Controle">
          <LN0 lnClass="LLN0" inst="" lnType="LLN0_T">
            <DataSet name="DS_ESTADOS_N2" desc="Estados N2">{inds}
              <FCDA ldInst="CTRL" prefix="" lnClass="XCBR" lnInst="1" doName="Pos" fc="ST"/>
              <FCDA ldInst="CTRL" prefix="" lnClass="XCBR" lnInst="1" doName="Loc" fc="ST"/>
            </DataSet>
            <DataSet name="DS_ESTADOS_SAGE" desc="Estados SAGE (exemplo para o teste T6)">
              <FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind3" fc="ST"/>
              <FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind4" fc="ST"/>
            </DataSet>
            <DataSet name="DS_PROT" desc="Proteção">
              <FCDA ldInst="CTRL" prefix="" lnClass="PTOC" lnInst="1" doName="Str" fc="ST"/>
              <FCDA ldInst="CTRL" prefix="" lnClass="PTOC" lnInst="1" doName="Op" fc="ST"/>
            </DataSet>
            <DataSet name="DS_MEDIDAS" desc="Medidas">
              <FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="TotW" fc="MX"/>
              <FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="Hz" fc="MX"/>
              <FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="PhV.phsA" fc="MX"/>
              <FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="A.phsA" fc="MX"/>
              <FCDA ldInst="MED" prefix="" lnClass="MMTR" lnInst="1" doName="TotWh" fc="ST"/>
            </DataSet>
            {rc("brcbEstados", "DS_ESTADOS_N2", True)}
            {rc("urcbEstados", "DS_ESTADOS_N2", False)}
            {rc("urcbSAGE", "DS_ESTADOS_SAGE", False)}
            {rc("brcbProt", "DS_PROT", True)}
            {rc("urcbMedidas", "DS_MEDIDAS", False, 3, 10000, "true", 100)}
            <DOI name="NamPlt"><DAI name="vendor"><Val>Ingeteam</Val></DAI><DAI name="swRev"><Val>exemplo</Val></DAI><DAI name="configRev"><Val>2026-10-09</Val></DAI></DOI>
          </LN0>
          <LN lnClass="LPHD" inst="1" lnType="LPHD_T"><DOI name="PhyNam"><DAI name="vendor"><Val>Ingeteam</Val></DAI><DAI name="model"><Val>INGEPAC EF</Val></DAI></DOI></LN>
          <LN prefix="sAut" lnClass="GGIO" inst="2" lnType="GGIO_T" desc="Automatismos 2">{dois}</LN>
          <LN lnClass="XCBR" inst="1" lnType="XCBR_T" desc="Disjuntor 52-H06"><DOI name="Pos" desc="DISJUNTOR 52-H06"/></LN>
          <LN lnClass="PTOC" inst="1" lnType="PTOC_T" desc="Sobrecorrente 50/51"/>
        </LDevice>
        <LDevice inst="MED" desc="Medição">
          <LN0 lnClass="LLN0" inst="" lnType="LLN0_T"/>
          <LN lnClass="MMXU" inst="1" lnType="MMXU_T" desc="Medidas"><DOI name="Hz" desc="Frequência"><SDI name="rangeC"><SDI name="min"><DAI name="f"><Val>45</Val></DAI></SDI><SDI name="max"><DAI name="f"><Val>65</Val></DAI></SDI></SDI><SDI name="mag"><DAI name="f"><Val>60</Val></DAI></SDI></DOI><DOI name="TotW" desc="Potência ativa total"/></LN>
          <LN lnClass="MMTR" inst="1" lnType="MMTR_T" desc="Energia"/>
        </LDevice>
      </Server>
    </AccessPoint>
  </IED>
{TEMPLATES}
</SCL>
"""


def i15_rcbs_reais() -> str:
    """i15 com os RCBs e DataSets nomeados como no CID real (BRCB_ESTADOS_N2/N3, BRCB_ESTADOS_SAGE não
    bufferizado com RptEnabled max=6, URCB_MEDIDAS_*), para os testes da política de acesso por RCB."""
    base = i15()
    ini = base.index('<DataSet name="DS_ESTADOS_N2"')
    fim = base.index('<DOI name="NamPlt">')
    inds = "".join(f'<FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind{i}" fc="ST"/>' for i in range(1, 9))
    sage = "".join(f'<FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind{i}" fc="ST"/>' for i in (3, 4))
    n3 = "".join(f'<FCDA ldInst="CTRL" prefix="sAut" lnClass="GGIO" lnInst="2" doName="Ind{i}" fc="ST"/>' for i in range(1, 5))
    med = ('<FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="TotW" fc="MX"/>'
           '<FCDA ldInst="MED" prefix="" lnClass="MMXU" lnInst="1" doName="Hz" fc="MX"/>')

    def rc(name, ds, buffered, maxn, intg, conf, buftime, period, dchg, entry, fc):
        b = "true" if buffered else "false"
        d = "true" if dchg else "false"
        return (f'<ReportControl name="{name}" rptID="CTRL/LLN0.{fc}.{name}" datSet="{ds}" buffered="{b}" intgPd="{intg}" '
                f'confRev="{conf}" bufTime="{buftime}"><TrgOps dchg="{d}" qchg="{d}" dupd="false" period="{period}"/>'
                f'<OptFields seqNum="true" timeStamp="true" dataSet="true" reasonCode="true" dataRef="false" '
                f'entryID="{entry}" configRef="false"/><RptEnabled max="{maxn}"/></ReportControl>')

    bloco = f"""<DataSet name="DS_ESTADOS_N2" desc="Estados N2">{inds}</DataSet>
            <DataSet name="DS_ESTADOS_N3" desc="Estados N3">{n3}</DataSet>
            <DataSet name="DS_ESTADOS_SAGE" desc="Estados SAGE">{sage}</DataSet>
            <DataSet name="DS_MEDIDAS_N2" desc="Medidas N2">{med}</DataSet>
            <DataSet name="DS_MEDIDAS_N3" desc="Medidas N3">{med}</DataSet>
            <DataSet name="DS_MEDIDAS_SAGE" desc="Medidas SAGE">{med}</DataSet>
            {rc("URCB_MEDIDAS_N2", "DS_MEDIDAS_N2", False, 0, 2000, 10001, 1000, "true", False, "false", "RP")}
            {rc("BRCB_ESTADOS_N2", "DS_ESTADOS_N2", True, 0, 0, 1, 100, "false", True, "true", "BR")}
            {rc("BRCB_ESTADOS_N3", "DS_ESTADOS_N3", True, 0, 0, 30001, 100, "false", True, "true", "BR")}
            {rc("URCB_MEDIDAS_N3", "DS_MEDIDAS_N3", False, 0, 2000, 30001, 1000, "true", False, "false", "RP")}
            {rc("BRCB_ESTADOS_SAGE", "DS_ESTADOS_SAGE", False, 6, 2000, 30001, 100, "true", False, "false", "RP")}
            {rc("URCB_MEDIDAS_SAGE", "DS_MEDIDAS_SAGE", False, 6, 2000, 30001, 1000, "true", False, "false", "RP")}
            """
    return base[:ini] + bloco + base[fim:]


def ucdqsa1() -> str:
    ied = "SE_BAB__UCDQSA1"

    def rc(name, ds, maxn=3):
        return (f'<ReportControl name="{name}" rptID="{ied}LD0/{name}" datSet="{ds}" confRev="1" buffered="false" '
                f'bufTime="0" intgPd="0" indexed="true"><TrgOps dchg="true" qchg="true" dupd="false" period="false" gi="true"/>'
                f'{OPT_ALL}<RptEnabled max="{maxn}"/></ReportControl>')

    inds = "".join(f'<FCDA ldInst="LD0" prefix="" lnClass="GGIO" lnInst="1" doName="Ind{i}" fc="ST"/>' for i in range(1, 5))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- CID SINTÉTICO de referência (Edição 2, RCB por LN). Não é o CID real do UCDQSA1. -->
<SCL xmlns="http://www.iec.ch/61850/2003/SCL" version="2007" revision="B" release="4">
  <Header id="UCDQSA1" toolID="gen_exemplos.py"/>
  <Communication>
    <SubNetwork name="SE_BAB_MMS" type="8-MMS">
      <ConnectedAP iedName="{ied}" apName="AP1"><Address><P type="IP">10.237.68.150</P></Address></ConnectedAP>
    </SubNetwork>
  </Communication>
  <IED name="{ied}" manufacturer="Referencia" type="UCD" configVersion="1">
    <Services>
      <DynAssociation max="5"/><GetDirectory/><GetDataObjectDefinition/><DataObjectDirectory/><GetDataSetValue/>
      <DataSetDirectory/><ConfDataSet max="32" maxAttributes="200"/><DynDataSet max="8"/><ReadWrite/>
      <ConfReportControl max="64"/><GetCBValues/>
      <ReportSettings cbName="Conf" datSet="Dyn" rptID="Dyn" optFields="Dyn" bufTime="Dyn" trgOps="Dyn" intgPd="Dyn" resvTms="true" owner="true"/>
    </Services>
    <AccessPoint name="AP1">
      <Server>
        <Authentication none="true"/>
        <LDevice inst="LD0">
          <LN0 lnClass="LLN0" inst="" lnType="LLN0_T"/>
          <LN lnClass="LPHD" inst="1" lnType="LPHD_T"/>
          <LN lnClass="GGIO" inst="1" lnType="GGIO_T" desc="Sinais">
            <DataSet name="DS_GGIO1">{inds}</DataSet>
            {rc("urcbGGIO1", "DS_GGIO1")}
            <DOI name="Ind3" desc="MOLA"/>
          </LN>
          <LN lnClass="XCBR" inst="1" lnType="XCBR_T">
            <DataSet name="DS_XCBR1"><FCDA ldInst="LD0" prefix="" lnClass="XCBR" lnInst="1" doName="Pos" fc="ST"/></DataSet>
            {rc("urcbXCBR1", "DS_XCBR1")}
          </LN>
        </LDevice>
      </Server>
    </AccessPoint>
  </IED>
{TEMPLATES}
</SCL>
"""


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    (here / "SE_BAB_H06_BCM1_i15_exemplo_ed1.cid").write_text(i15(), encoding="utf-8")
    (here / "SE_BAB_UCDQSA1_referencia_exemplo_ed2.cid").write_text(ucdqsa1(), encoding="utf-8")
    (here / "SE_BAB_H06_BCM1_i15_rcbs_reais_ed1.cid").write_text(i15_rcbs_reais(), encoding="utf-8")
    print("ok")
